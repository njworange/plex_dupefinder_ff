import traceback


setting = {
    "filepath": __file__,
    "use_db": True,
    "use_default_setting": True,
    "home_module": "cleanup",
    "menu": {
        "uri": __package__,
        "name": "PLEX DupeFinder",
        "list": [
            {
                "uri": "setting",
                "name": "설정",
                "list": [{"uri": "setting", "name": "설정"}],
            },
            {
                "uri": "cleanup",
                "name": "중복 자동 정리",
                "list": [{"uri": "run", "name": "실행"}],
            },
            {
                "uri": "history",
                "name": "작업 이력",
                "list": [{"uri": "list", "name": "이력"}],
            },
            {"uri": "log", "name": "로그"},
        ],
    },
    "setting_menu": None,
    "default_route": "normal",
}


from plugin import *  # noqa: E402,F401,F403


P = create_plugin_instance(setting)

try:
    # FlaskFarm must see the models before its create_all phase.
    from .models import ModelCleanupAction, ModelCleanupRun
    from .mod_cleanup import ModuleCleanup
    from .mod_history import ModuleHistory
    from .mod_setting import ModuleSetting

    P.ModelCleanupRun = ModelCleanupRun
    P.ModelCleanupAction = ModelCleanupAction
    P.set_module_list([ModuleSetting, ModuleCleanup, ModuleHistory])
except Exception as exc:
    P.logger.error("Exception:%s", str(exc))
    P.logger.error(traceback.format_exc())


logger = P.logger
