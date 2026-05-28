"""Tests for startup.py — environment validation at boot time."""

import os
from unittest.mock import patch

import pytest


class TestValidateRequiredEnv:
    def test_all_vars_set_passes(self):
        from startup import validate_required_env
        env = {
            "APP_API_KEY": "key1",
            "HYDRADB_API_KEY": "key2",
            "HYDRADB_TENANT_ID": "tenant",
            "OPENAI_API_KEY": "key3",
        }
        with patch.dict(os.environ, env, clear=False):
            validate_required_env()  # should not raise

    @pytest.mark.parametrize("missing_var", [
        "APP_API_KEY",
        "HYDRADB_API_KEY",
        "HYDRADB_TENANT_ID",
        "OPENAI_API_KEY",
    ])
    def test_missing_var_raises(self, missing_var):
        from startup import validate_required_env, StartupConfigError
        env = {
            "APP_API_KEY": "key1",
            "HYDRADB_API_KEY": "key2",
            "HYDRADB_TENANT_ID": "tenant",
            "OPENAI_API_KEY": "key3",
        }
        env.pop(missing_var)
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(StartupConfigError):
                validate_required_env()

    def test_blank_var_raises(self):
        from startup import validate_required_env, StartupConfigError
        with patch.dict(os.environ, {"APP_API_KEY": "  "}, clear=False):
            # Blank (whitespace-only) should still be caught
            env_backup = os.environ.get("APP_API_KEY")
            os.environ["APP_API_KEY"] = "  "
            try:
                with patch.dict(os.environ, {
                    "APP_API_KEY": "  ",
                    "HYDRADB_API_KEY": "k",
                    "HYDRADB_TENANT_ID": "t",
                    "OPENAI_API_KEY": "o",
                }, clear=True):
                    with pytest.raises(StartupConfigError):
                        validate_required_env()
            finally:
                if env_backup is not None:
                    os.environ["APP_API_KEY"] = env_backup

    def test_error_message_names_missing_vars(self):
        from startup import validate_required_env, StartupConfigError
        with patch.dict(os.environ, {
            "HYDRADB_API_KEY": "k",
            "HYDRADB_TENANT_ID": "t",
            "OPENAI_API_KEY": "o",
        }, clear=True):
            with pytest.raises(StartupConfigError) as exc_info:
                validate_required_env()
        assert "APP_API_KEY" in str(exc_info.value)

    def test_startup_config_error_is_runtime_error(self):
        from startup import StartupConfigError
        assert issubclass(StartupConfigError, RuntimeError)

    def test_required_env_vars_list(self):
        from startup import REQUIRED_ENV_VARS
        assert "APP_API_KEY" in REQUIRED_ENV_VARS
        assert "HYDRADB_API_KEY" in REQUIRED_ENV_VARS
        assert "HYDRADB_TENANT_ID" in REQUIRED_ENV_VARS
        assert "OPENAI_API_KEY" in REQUIRED_ENV_VARS


# ── Gmail OAuth env group validation ─────────────────────────────────────
# Gmail Connect is opt-in: the deployer can either set ALL four Gmail
# OAuth env vars or NONE of them. A partial config is the dangerous
# state — the app boots clean but crashes at first user click. These
# tests pin that group-validation contract at startup.
class TestGmailEnvGroupValidation:
    # Minimal env that makes the REQUIRED_ENV_VARS check pass. We start
    # from this in every test and then mutate the 4 Gmail vars.
    _BASE_ENV = {
        "APP_API_KEY":               "k",
        "HYDRADB_API_KEY":           "k",
        "HYDRADB_TENANT_ID":         "t",
        "OPENAI_API_KEY":            "k",
        "SUPABASE_URL":              "https://x.supabase.co",
        "SUPABASE_JWT_SECRET":       "s",
        "SUPABASE_SERVICE_ROLE_KEY": "s",
        "SLACK_CLIENT_ID":           "c",
        "SLACK_CLIENT_SECRET":       "s",
        "SLACK_REDIRECT_URI":        "http://localhost/x",
        "SLACK_OAUTH_STATE_SECRET":  "x",
        "SLACK_SIGNING_SECRET":      "x",
    }
    _GMAIL_GROUP = (
        "GMAIL_CLIENT_ID",
        "GMAIL_CLIENT_SECRET",
        "GMAIL_REDIRECT_URI",
        "GMAIL_OAUTH_STATE_SECRET",
    )

    def test_none_set_boots_normally(self):
        """All 4 Gmail vars unset → Gmail is opted-out, no error."""
        from startup import validate_required_env
        with patch.dict(os.environ, self._BASE_ENV, clear=True):
            validate_required_env()  # should not raise

    def test_all_set_boots_normally(self):
        """All 4 Gmail vars set → Gmail is enabled, no error."""
        from startup import validate_required_env
        env = dict(self._BASE_ENV)
        env.update({
            "GMAIL_CLIENT_ID":          "gid",
            "GMAIL_CLIENT_SECRET":      "gsec",
            "GMAIL_REDIRECT_URI":       "https://api/x/gmail/oauth/callback",
            "GMAIL_OAUTH_STATE_SECRET": "gstate",
        })
        with patch.dict(os.environ, env, clear=True):
            validate_required_env()  # should not raise

    @pytest.mark.parametrize("missing_var", _GMAIL_GROUP)
    def test_three_set_one_missing_raises(self, missing_var):
        """The dangerous case: any 3 set, one missing → partial config →
        StartupConfigError naming the missing one."""
        from startup import validate_required_env, StartupConfigError
        env = dict(self._BASE_ENV)
        env.update({
            "GMAIL_CLIENT_ID":          "gid",
            "GMAIL_CLIENT_SECRET":      "gsec",
            "GMAIL_REDIRECT_URI":       "https://api/x/gmail/oauth/callback",
            "GMAIL_OAUTH_STATE_SECRET": "gstate",
        })
        del env[missing_var]
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(StartupConfigError) as exc_info:
                validate_required_env()
        assert missing_var in str(exc_info.value)
        # The error message must guide the operator: tell them what
        # the alternative (full opt-out) looks like.
        assert "partially configured" in str(exc_info.value).lower()

    def test_one_set_three_missing_raises(self):
        """The other partial case — single Gmail var set, rest blank."""
        from startup import validate_required_env, StartupConfigError
        env = dict(self._BASE_ENV)
        env["GMAIL_CLIENT_ID"] = "gid"  # only one set
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(StartupConfigError) as exc_info:
                validate_required_env()
        msg = str(exc_info.value)
        # All three of the still-blank Gmail vars should be named.
        assert "GMAIL_CLIENT_SECRET" in msg
        assert "GMAIL_REDIRECT_URI" in msg
        assert "GMAIL_OAUTH_STATE_SECRET" in msg

    def test_blank_string_counts_as_unset(self):
        """The validator treats a blank/whitespace value as missing —
        same convention as REQUIRED_ENV_VARS. Confirms a deployer who
        defines the var but leaves it empty is treated as opted-out
        (not partially configured), when ALL four are blank."""
        from startup import validate_required_env
        env = dict(self._BASE_ENV)
        env.update({
            "GMAIL_CLIENT_ID":          "",
            "GMAIL_CLIENT_SECRET":      "   ",
            "GMAIL_REDIRECT_URI":       "",
            "GMAIL_OAUTH_STATE_SECRET": "",
        })
        with patch.dict(os.environ, env, clear=True):
            validate_required_env()  # should not raise

    def test_validator_helper_returns_correct_shape(self):
        """Unit-level: the helper distinguishes none / all / partial."""
        from startup import _validate_gmail_group
        # All unset
        with patch.dict(os.environ, self._BASE_ENV, clear=True):
            assert _validate_gmail_group() == []
        # All set
        env = dict(self._BASE_ENV)
        env.update({
            "GMAIL_CLIENT_ID":          "gid",
            "GMAIL_CLIENT_SECRET":      "gsec",
            "GMAIL_REDIRECT_URI":       "x",
            "GMAIL_OAUTH_STATE_SECRET": "y",
        })
        with patch.dict(os.environ, env, clear=True):
            assert _validate_gmail_group() == []
        # Partial: 2 set, 2 missing
        env = dict(self._BASE_ENV)
        env.update({
            "GMAIL_CLIENT_ID":     "gid",
            "GMAIL_CLIENT_SECRET": "gsec",
        })
        with patch.dict(os.environ, env, clear=True):
            missing = _validate_gmail_group()
            assert set(missing) == {"GMAIL_REDIRECT_URI", "GMAIL_OAUTH_STATE_SECRET"}