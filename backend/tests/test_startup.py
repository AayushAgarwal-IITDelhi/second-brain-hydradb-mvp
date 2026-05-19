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

    @pytest.mark.parametrize(
        "missing_var",
        [
            "APP_API_KEY",
            "HYDRADB_API_KEY",
            "HYDRADB_TENANT_ID",
            "OPENAI_API_KEY",
        ],
    )
    def test_missing_var_raises(self, missing_var):
        from startup import StartupConfigError, validate_required_env

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
        from startup import StartupConfigError, validate_required_env

        with patch.dict(os.environ, {"APP_API_KEY": "  "}, clear=False):
            # Blank (whitespace-only) should still be caught
            env_backup = os.environ.get("APP_API_KEY")
            os.environ["APP_API_KEY"] = "  "
            try:
                with patch.dict(
                    os.environ,
                    {
                        "APP_API_KEY": "  ",
                        "HYDRADB_API_KEY": "k",
                        "HYDRADB_TENANT_ID": "t",
                        "OPENAI_API_KEY": "o",
                    },
                    clear=True,
                ):
                    with pytest.raises(StartupConfigError):
                        validate_required_env()
            finally:
                if env_backup is not None:
                    os.environ["APP_API_KEY"] = env_backup

    def test_error_message_names_missing_vars(self):
        from startup import StartupConfigError, validate_required_env

        with patch.dict(
            os.environ,
            {
                "HYDRADB_API_KEY": "k",
                "HYDRADB_TENANT_ID": "t",
                "OPENAI_API_KEY": "o",
            },
            clear=True,
        ):
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
