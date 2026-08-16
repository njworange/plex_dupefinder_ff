from __future__ import annotations

import sys
import types
import unittest
from unittest import mock

from test_flaskfarm_compat import FlaskFarmImportHarness, PACKAGE_NAME


class DeleteBudgetContractTest(unittest.TestCase):
    def _module(self):
        return sys.modules[PACKAGE_NAME + ".delete_budget"]

    def test_missing_empty_invalid_and_persisted_one_fail_closed_to_one(self) -> None:
        with FlaskFarmImportHarness() as harness:
            module = self._module()
            setting_module = harness.setup_module.P.module_list[0]
            self.assertEqual(
                setting_module.db_default["setting_max_delete_per_run"], "1"
            )
            settings = harness.setup_module.P.ModelSetting._data
            cases = (
                ({}, 1),
                ({"setting_max_delete_per_run": ""}, 1),
                ({"setting_max_delete_per_run": "not-an-integer"}, 1),
                ({"setting_max_delete_per_run": "1"}, 1),
                ({"setting_max_delete_per_run": "0"}, 1),
                ({"setting_max_delete_per_run": "101"}, 100),
            )
            for values, expected in cases:
                with self.subTest(values=values), mock.patch.dict(
                    settings, values, clear=True
                ):
                    self.assertEqual(module.current_delete_attempt_limit(), expected)

    def test_budget_is_live_and_never_rewrites_a_persisted_setting(self) -> None:
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
                    {"limit": 1, "attempted": 1, "remaining": 0, "exhausted": True},
                )
                self.assertEqual(settings["setting_max_delete_per_run"], "1")

                settings["setting_max_delete_per_run"] = "2"
                second = module.delete_attempt_budget(run)
                self.assertEqual(
                    second,
                    {"limit": 2, "attempted": 1, "remaining": 1, "exhausted": False},
                )
                self.assertEqual(settings["setting_max_delete_per_run"], "2")

    def test_corrupt_attempt_counter_fails_closed_and_message_is_actionable(self) -> None:
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
                        self.assertEqual(budget["attempted"], 2)
                        self.assertEqual(budget["remaining"], 0)
                        self.assertTrue(budget["exhausted"])
                message = module.delete_attempt_limit_message(budget)
                self.assertIn("사용 2/2", message)
                self.assertIn("남음 0", message)
                self.assertIn("설정 > 삭제 안전장치", message)
                self.assertIn("새 중복 검사", message)


if __name__ == "__main__":
    unittest.main()
