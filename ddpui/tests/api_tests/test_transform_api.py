from pathlib import Path
from pydantic.error_wrappers import ValidationError as PydanticValidationError
import pydantic
import pytest
from unittest.mock import Mock, patch
from datetime import datetime
from ninja.errors import HttpError, ValidationError
from ddpui.models.role_based_access import Role, RolePermission, Permission
from ddpui.models.org_user import OrgUser
from ddpui.models.org import OrgWarehouse, OrgDbt
from ddpui.auth import (
    GUEST_ROLE,
    SUPER_ADMIN_ROLE,
    ACCOUNT_MANAGER_ROLE,
    PIPELINE_MANAGER_ROLE,
    ANALYST_ROLE,
)
from ddpui.schemas.org_task_schema import DbtProjectSchema
from ddpui.tests.api_tests.test_user_org_api import (
    seed_db,
    orguser,
    nonadminorguser,
    mock_request,
    authuser,
    org_without_workspace,
)
from ddpui.api.transform_api import (
    create_dbt_project,
    delete_dbt_project,
    sync_sources,
    post_construct_dbt_model_operation,
    put_operation,
    get_operation,
    post_save_model,
    get_input_sources_and_models,
    get_dbt_project_DAG,
    delete_model,
    delete_operation,
    get_warehouse_datatypes,
    post_lock_canvas,
    LockCanvasRequestSchema,
    CanvasLock,
    post_unlock_canvas,
)

pytestmark = pytest.mark.django_db


def test_seed_data(seed_db):
    """a test to seed the database"""
    assert Role.objects.count() == 5
    assert RolePermission.objects.count() > 5
    assert Permission.objects.count() > 5


def test_post_lock_canvas_acquire(orguser):
    """a test to lock the canvas"""
    request = mock_request(orguser)
    payload = LockCanvasRequestSchema()
    response = post_lock_canvas(request, payload)
    assert response is not None
    assert response.locked_by == orguser.user.email
    assert response.lock_id is not None


def test_post_lock_canvas_locked_by_another(orguser, nonadminorguser):
    """a test to lock the canvas"""
    CanvasLock.objects.create(locked_by=nonadminorguser, locked_at=datetime.now())
    request = mock_request(orguser)
    payload = LockCanvasRequestSchema()
    response = post_lock_canvas(request, payload)
    assert response is not None
    assert response.locked_by == nonadminorguser.user.email
    assert response.lock_id is None


def test_post_lock_canvas_locked_by_self(orguser):
    """a test to lock the canvas"""
    request = mock_request(orguser)
    payload = LockCanvasRequestSchema()
    post_lock_canvas(request, payload)
    # the second call won't acquire a lock
    response = post_lock_canvas(request, payload)
    assert response is not None
    assert response.locked_by == orguser.user.email
    assert response.lock_id is None


def test_post_unlock_canvas_unlocked(orguser):
    """a test to unlock the canvas"""
    request = mock_request(orguser)
    payload = LockCanvasRequestSchema()
    with pytest.raises(HttpError) as excinfo:
        post_unlock_canvas(request, payload)
    assert str(excinfo.value) == "no lock found"


def test_post_unlock_canvas_locked(orguser):
    """a test to unlock the canvas"""
    request = mock_request(orguser)
    payload = LockCanvasRequestSchema()
    post_lock_canvas(request, payload)

    with pytest.raises(HttpError) as excinfo:
        post_unlock_canvas(request, payload)
    assert str(excinfo.value) == "wrong lock id"


def test_post_unlock_canvas_locked_unlocked(orguser):
    """a test to unlock the canvas"""
    request = mock_request(orguser)
    payload = LockCanvasRequestSchema()
    lock = post_lock_canvas(request, payload)

    payload.lock_id = lock.lock_id
    response = post_unlock_canvas(request, payload)
    assert response == {"success": 1}


def test_post_unlock_canvas_locked_wrong_lock_id(orguser):
    """a test to unlock the canvas"""
    request = mock_request(orguser)
    payload = LockCanvasRequestSchema()
    post_lock_canvas(request, payload)

    payload.lock_id = "wrong-lock-id"
    with pytest.raises(HttpError) as excinfo:
        post_unlock_canvas(request, payload)
    assert str(excinfo.value) == "wrong lock id"


# =================================== ui4t =======================================


def test_create_dbt_project_check_permission(orguser: OrgUser):
    """
    a failure test to check if the orguser has the correct permission
    """
    slugs_have_permission = [
        SUPER_ADMIN_ROLE,
        ACCOUNT_MANAGER_ROLE,
        ANALYST_ROLE,
        PIPELINE_MANAGER_ROLE,
    ]
    slugs_dont_have_permission = [GUEST_ROLE]

    payload = DbtProjectSchema(default_schema="default")
    for slug in slugs_dont_have_permission:
        orguser.new_role = Role.objects.filter(slug=slug).first()
        request = mock_request(orguser)
        with pytest.raises(HttpError) as exc:
            create_dbt_project(request, payload)
        assert str(exc.value) == "unauthorized"

    for slug in slugs_have_permission:
        orguser.new_role = Role.objects.filter(slug=slug).first()
        request = mock_request(orguser)
        with pytest.raises(HttpError) as exc:
            create_dbt_project(request, payload)
        assert str(exc.value) == "Please set up your warehouse first"


def test_create_dbt_project_mocked_helper(orguser: OrgUser):
    """a success test to create a dbt project"""
    request = mock_request(orguser)
    payload = DbtProjectSchema(default_schema="default")

    with patch(
        "ddpui.api.transform_api.setup_local_dbt_workspace",
        return_value=(None, None),
    ) as setup_local_dbt_workspace_mock:
        result = create_dbt_project(request, payload)
        assert isinstance(result, dict)
        assert "message" in result
        assert result["message"] == f"Project {orguser.org.slug} created successfully"
        setup_local_dbt_workspace_mock.assert_called_once_with(
            orguser.org, project_name="dbtrepo", default_schema=payload.default_schema
        )


def test_delete_dbt_project_failure_projectdir_does_not_exist(
    orguser: OrgUser, tmp_path
):
    """a failure test for delete a dbt project api when project dir does not exist"""
    request = mock_request(orguser)
    project_name = "dummy-project"
    with patch("os.getenv", return_value=tmp_path):
        with pytest.raises(HttpError) as excinfo:
            delete_dbt_project(request, project_name)
    assert (
        str(excinfo.value)
        == f"Organization {orguser.org.slug} does not have any projects"
    )


def test_delete_dbt_project_failure_dbtrepodir_does_not_exist(
    orguser: OrgUser, tmp_path
):
    """a failure test for delete a dbt project api when dbt repo dir does not exist"""
    request = mock_request(orguser)
    project_name = "dummy-project"
    with patch("os.getenv", return_value=tmp_path):
        project_dir = Path(tmp_path) / orguser.org.slug
        project_dir.mkdir(parents=True, exist_ok=True)
        with pytest.raises(HttpError) as excinfo:
            delete_dbt_project(request, project_name)
    assert (
        str(excinfo.value)
        == f"Project {project_name} does not exist in organization {orguser.org.slug}"
    )


def test_delete_dbt_project_success(orguser: OrgUser, tmp_path):
    """a failure test for delete a dbt project api when dbt repo dir does not exist"""
    request = mock_request(orguser)
    project_name = "dummy-project"

    project_dir = Path(tmp_path) / orguser.org.slug
    project_dir.mkdir(parents=True, exist_ok=True)
    dbtrepo_dir = project_dir / project_name
    dbtrepo_dir.mkdir(parents=True, exist_ok=True)
    dbt = OrgDbt.objects.create(
        project_dir=str(project_dir),
        dbt_venv="some/venv/bin/dbt",
        target_type="postgres",
        default_schema="default",
        transform_type="github",
    )
    orguser.org.dbt = dbt
    orguser.org.save()

    assert OrgDbt.objects.filter(org=orguser.org).count() == 1

    with patch("os.getenv", return_value=tmp_path):
        delete_dbt_project(request, project_name)

    assert not dbtrepo_dir.exists()
    assert project_dir.exists()
    assert OrgDbt.objects.filter(org=orguser.org).count() == 0


def test_sync_sources_failed_warehouse_not_present(orguser: OrgUser):
    """a failure test for sync sources when warehouse is not present"""
    request = mock_request(orguser)
    with pytest.raises(HttpError) as excinfo:
        sync_sources(request)
    assert str(excinfo.value) == "Please set up your warehouse first"


def test_sync_sources_failed_dbt_workspace_not_setup(orguser: OrgUser):
    """a failure test for sync sources when dbt workspace is not setup"""
    OrgWarehouse.objects.create(
        org=orguser.org,
        wtype="postgres",
        airbyte_destination_id="airbyte_destination_id",
    )
    request = mock_request(orguser)
    with pytest.raises(HttpError) as excinfo:
        sync_sources(request)
    assert str(excinfo.value) == "DBT workspace not set up"


def test_sync_sources_success(orguser: OrgUser, tmp_path):
    """a success test for sync sources"""
    org_warehouse = OrgWarehouse.objects.create(
        org=orguser.org,
        wtype="postgres",
        airbyte_destination_id="airbyte_destination_id",
    )
    orgdbt = OrgDbt.objects.create(
        gitrepo_url=None,
        project_dir=str(Path(tmp_path) / orguser.org.slug),
        dbt_venv=tmp_path,
        target_type="postgres",
        default_schema="default_schema",
        transform_type="ui",
    )
    orguser.org.dbt = orgdbt
    orguser.org.save()

    request = mock_request(orguser)
    mocked_task = Mock()
    mocked_task.id = "task-id"
    with patch(
        "ddpui.core.dbtautomation_service.sync_sources_for_warehouse.delay",
        return_value=mocked_task,
    ) as delay:
        result = sync_sources(request)

        delay.assert_called_once_with(orgdbt.id, org_warehouse.id, orguser.org.slug)
        assert result["task_id"] == "task-id"
