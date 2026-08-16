from __future__ import annotations

import sys
import types
import unittest
from unittest import mock

from test_flaskfarm_compat import FlaskFarmImportHarness, PACKAGE_NAME


class DeleteBudgetContractTest(unittest.TestCase):
    def _module(self):
        return sys.modules[PACKAGE_NAME + ".delete_budget"]

    def test_legacy_limit_setting_is_not_registered_or_read(self) -> None:
        with FlaskFarmImportHarness() as harness:
            module = self._module()
            setting_module = harness.setup_module.P.module_list[0]
            self.assertNotIn("setting_max_delete_per_run", setting_module.db_default)
            settings = harness.setup_module.P.ModelSetting._data
            with mock.patch.dict(
                settings, {"setting_max_delete_per_run": "1"}, clear=True
            ):
                self.assertIsNone(module.current_delete_attempt_limit())
                self.assertEqual(settings["setting_max_delete_per_run"], "1")

    def test_budget_is_unlimited_and_never_rewrites_a_legacy_setting(self) -> None:
        with FlaskFarmImportHarness() as harness:
            module = self._module()
            settings = harness.setup_module.P.ModelSetting._data
            run = types.SimpleNamespace(deletion_attempts=1)

            with mock.patch.dict(
                settings, {"setting_max_delete_per_run": "1"}, clear=True
            ):
                first = module.delete_attempt_budget(run)
                self.assertEqual(
                    first,
                    {
                        "unlimited": True,
                        "attempted": 1,
                        "limit": None,
                        "remaining": None,
                        "exhausted": False,
                    },
                )
                self.assertEqual(settings["setting_max_delete_per_run"], "1")

                settings["setting_max_delete_per_run"] = "0"
                run.deletion_attempts = 500
                second = module.delete_attempt_budget(run)
                self.assertEqual(
                    second,
                    {
                        "unlimited": True,
                        "attempted": 500,
                        "limit": None,
                        "remaining": None,
                        "exhausted": False,
                    },
                )
                self.assertEqual(settings["setting_max_delete_per_run"], "0")

    def test_corrupt_attempt_counter_is_sanitized_without_creating_a_cap(self) -> None:
        with FlaskFarmImportHarness() as harness:
            module = self._module()
            with mock.patch.dict(
                harness.setup_module.P.ModelSetting._data,
                {"setting_max_delete_per_run": "2"},
                clear=True,
            ):
                for corrupt in ("corrupt", -1):
                    with self.subTest(corrupt=corrupt):
                        budget = module.delete_attempt_budget(
                            types.SimpleNamespace(deletion_attempts=corrupt)
                        )
                        self.assertEqual(budget["attempted"], 0)
                        self.assertIsNone(budget["limit"])
                        self.assertIsNone(budget["remaining"])
                        self.assertFalse(budget["exhausted"])
                        self.assertTrue(budget["unlimited"])
                message = module.delete_attempt_limit_message(budget)
                self.assertIn("무제한", message)
                self.assertIn("현재 시도 0회", message)
                self.assertEqual(module.require_delete_attempt_available(object()), {
                    "unlimited": True,
                    "attempted": 0,
                    "limit": None,
                    "remaining": None,
                    "exhausted": False,
                })


if __name__ == "__main__":
    unittest.main()
