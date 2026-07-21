from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = ROOT / "scripts" / "deploy-docker.ps1"


def test_compose_ports_can_be_overridden_without_changing_local_defaults():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert '${BACKEND_PORT:-8010}:8000' in compose
    assert '${FRONTEND_PORT:-8080}:80' in compose


def test_deploy_script_has_expected_server_configuration():
    script = SCRIPT_PATH.read_text(encoding="utf-8")

    assert '$ServerHost = "110.40.130.31"' in script
    assert "$SshPort = 22" in script
    assert '$RemoteDirectory = "/usr/travel/app"' in script
    assert '$GitRepository = "https://github.com/xiaozhaoben/travel_assistant.git"' in script
    assert '$GitBranch = "master"' in script
    assert '$ComposeProject = "travel"' in script


def test_deploy_script_updates_safely_and_uploads_both_environment_files():
    script = SCRIPT_PATH.read_text(encoding="utf-8")

    required = (
        'git -C "$REMOTE_DIR" pull --ff-only origin master',
        "git clone --branch master --single-branch",
        "backend/.env",
        "frontend/.env",
        "--env-file frontend/.env",
        "chmod 600",
        "docker compose -p travel",
        "--remove-orphans",
        "BACKEND_PORT=8081",
        "FRONTEND_PORT=8080",
        "/api/health",
    )
    for fragment in required:
        assert fragment in script

    assert "reset --hard" not in script
    assert "git clean" not in script
    assert "$Password =" not in script


def test_deploy_script_cleans_up_temporary_archives_on_both_hosts():
    script = SCRIPT_PATH.read_text(encoding="utf-8")

    assert "try {" in script
    assert "finally {" in script
    assert "Remove-Item -LiteralPath $ArchivePath -Force" in script
    assert "trap 'rm -f -- \"$REMOTE_ARCHIVE\"' EXIT" in script


def test_remote_bash_scripts_are_normalized_to_lf_before_transport():
    script = SCRIPT_PATH.read_text(encoding="utf-8")

    assert "function Invoke-RemoteBash" in script
    assert '.Replace("`r`n", "`n").Replace("`r", "`n")' in script
    assert "[Convert]::ToBase64String" in script
    assert "base64 --decode | bash" in script
    assert '$BootstrapScript | & ssh' not in script
    assert '$DeployScript | & ssh' not in script
