import importlib.util
import json
import sys
from pathlib import Path

SCRIPT_PATH = Path(
    "/home/maksym/Work/proj/Problemologist/Problemologist-AI/.autopilot/scripts/bmad-autopilot.py"
)


def _load_autopilot_module():
    spec = importlib.util.spec_from_file_location(
        "bmad_autopilot_cockpit_switch", SCRIPT_PATH
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_int_cockpit_codex_switcher_rotates_to_healthier_account(tmp_path):
    mod = _load_autopilot_module()

    data_dir = tmp_path / "com.antigravity.cockpit-tools"
    accounts_dir = data_dir / "codex_accounts"
    accounts_dir.mkdir(parents=True)

    current = mod.CockpitCodexAccount(
        id="acc-1",
        email="first@example.com",
        account_id="acct-1",
        quota=mod.CockpitCodexQuota(
            hourly_percentage=10,
            weekly_percentage=12,
            hourly_window_present=True,
            weekly_window_present=True,
        ),
        tokens=mod.CockpitCodexTokens(
            id_token="id-1",
            access_token="access-1",
            refresh_token="refresh-1",
            account_id="acct-1",
        ),
        created_at=100,
        last_used=100,
    )
    target = mod.CockpitCodexAccount(
        id="acc-2",
        email="second@example.com",
        account_id="acct-2",
        quota=mod.CockpitCodexQuota(
            hourly_percentage=88,
            weekly_percentage=91,
            hourly_window_present=True,
            weekly_window_present=True,
        ),
        tokens=mod.CockpitCodexTokens(
            id_token="id-2",
            access_token="access-2",
            refresh_token="refresh-2",
            account_id="acct-2",
        ),
        created_at=200,
        last_used=200,
    )

    for account in (current, target):
        (accounts_dir / f"{account.id}.json").write_text(
            json.dumps(mod.to_jsonable(account), indent=2) + "\n",
            encoding="utf-8",
        )

    (data_dir / "codex_accounts.json").write_text(
        json.dumps(
            {
                "version": "1.0",
                "accounts": [
                    {
                        "id": current.id,
                        "email": current.email,
                        "created_at": current.created_at,
                        "last_used": current.last_used,
                    },
                    {
                        "id": target.id,
                        "email": target.email,
                        "created_at": target.created_at,
                        "last_used": target.last_used,
                    },
                ],
                "current_account_id": current.id,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (data_dir / "auth.json").write_text(
        json.dumps(mod.build_cockpit_auth_file_value(current), indent=2) + "\n",
        encoding="utf-8",
    )

    switcher = mod.CockpitCodexSwitcher(
        lambda _message: None,
        mod.CockpitCodexSwitchSettings(
            mode="auto", primary_threshold=20, secondary_threshold=20
        ),
    )

    switched = switcher.maybe_switch(str(data_dir))

    assert switched is not None
    assert switched.id == target.id

    auth_payload = json.loads((data_dir / "auth.json").read_text(encoding="utf-8"))
    assert auth_payload["tokens"]["account_id"] == "acct-2"

    index_payload = json.loads(
        (data_dir / "codex_accounts.json").read_text(encoding="utf-8")
    )
    assert index_payload["current_account_id"] == target.id
