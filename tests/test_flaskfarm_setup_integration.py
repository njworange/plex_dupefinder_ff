from __future__ import annotations

import importlib
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

from flask import Flask
from flask_sqlalchemy import SQLAlchemy


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "_pdff_setup_test"


class _Logger:
    def __getattr__(self, name):
        del name
        return lambda *args, **kwargs: None


class _ModelSetting:
    values = {}

    @classmethod
    def get(cls, key):
        return cls.values.get(key)

    @classmethod
    def set(cls, key, value):
        cls.values[key] = value

    @classmethod
    def to_dict(cls):
        return dict(cls.values)


class _PluginModuleBase:
    def __init__(self, plugin, name=None, first_menu=None, scheduler_desc=None):
        self.plugin = plugin
        self.name = name
        self.first_menu = first_menu
        self.scheduler_desc = scheduler_desc


class FlaskFarmSetupIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.saved = {
            name: sys.modules.get(name)
            for name in ("framework", "plugin", PACKAGE)
        }
        app = Flask(__name__)
        app.config.update(
            TESTING=True,
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_BINDS={"plex_dupefinder_ff": "sqlite:///:memory:"},
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        self.app = app
        self.db = SQLAlchemy(app)

        framework = types.ModuleType("framework")
        framework.F = SimpleNamespace(
            app=app,
            db=self.db,
            PluginManager=SimpleNamespace(),
        )

        plugin = types.ModuleType("plugin")
        plugin.ModelBase = self.db.Model
        plugin.PluginModuleBase = _PluginModuleBase

        def create_plugin_instance(setting):
            instance = SimpleNamespace(
                setting=setting,
                package_name="plex_dupefinder_ff",
                ModelSetting=_ModelSetting,
                logger=_Logger(),
                module_list=[],
            )
            instance.set_module_list = lambda modules: setattr(
                instance, "module_list", list(modules)
            )
            return instance

        plugin.create_plugin_instance = create_plugin_instance

        package = types.ModuleType(PACKAGE)
        package.__path__ = [str(ROOT)]
        sys.modules.update(
            {
                "framework": framework,
                "plugin": plugin,
                PACKAGE: package,
            }
        )

    def tearDown(self):
        with self.app.app_context():
            self.db.session.remove()
        for name in tuple(sys.modules):
            if name == PACKAGE or name.startswith(PACKAGE + "."):
                sys.modules.pop(name, None)
        for name, value in self.saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value

    def test_full_setup_import_registers_two_models_and_three_modules(self):
        setup = importlib.import_module(PACKAGE + ".setup")
        self.assertEqual(setup.P.ModelCleanupRun.__tablename__, "plex_dupefinder_ff_cleanup_run")
        self.assertEqual(
            setup.P.ModelCleanupAction.__tablename__,
            "plex_dupefinder_ff_cleanup_action",
        )
        self.assertEqual(
            [module.__name__ for module in setup.P.module_list],
            ["ModuleSetting", "ModuleCleanup", "ModuleHistory"],
        )
        with self.app.app_context():
            self.db.create_all()
            table_names = set(
                self.db.inspect(
                    self.db.engines["plex_dupefinder_ff"]
                ).get_table_names()
            )
        self.assertEqual(
            table_names,
            {
                "plex_dupefinder_ff_cleanup_action",
                "plex_dupefinder_ff_cleanup_run",
            },
        )


if __name__ == "__main__":
    unittest.main()
