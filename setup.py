import traceback


setting = {
    "filepath": __file__,
    "use_db": True,
    "use_default_setting": True,
    "home_module": "scan",
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
                "uri": "scan",
                "name": "중복 검사",
                "list": [
                    {"uri": "list", "name": "스캔"},
                    {"uri": "results", "name": "결과"},
                ],
            },
            {
                "uri": "history",
                "name": "작업 이력",
                "list": [{"uri": "list", "name": "삭제 이력"}],
            },
            {
                "uri": "manual",
                "name": "매뉴얼",
                "list": [{"uri": "README.md", "name": "README.md"}],
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
    # Models must be imported before FlaskFarm calls db.create_all().
    from .models import (
        ModelActionLog,
        ModelBatchItem,
        ModelBatchRun,
        ModelDeletionLease,
        ModelDirectDeleteJournal,
        ModelDuplicateGroup,
        ModelMediaCandidate,
        ModelPostDeleteScanJob,
        ModelQuarantineJournal,
        ModelScanRun,
    )
    from .mod_history import ModuleHistory
    from .mod_scan import ModuleScan
    from .mod_setting import ModuleSetting

    P.ModelScanRun = ModelScanRun
    P.ModelDuplicateGroup = ModelDuplicateGroup
    P.ModelMediaCandidate = ModelMediaCandidate
    P.ModelActionLog = ModelActionLog
    P.ModelBatchRun = ModelBatchRun
    P.ModelBatchItem = ModelBatchItem
    P.ModelDeletionLease = ModelDeletionLease
    P.ModelDirectDeleteJournal = ModelDirectDeleteJournal
    P.ModelPostDeleteScanJob = ModelPostDeleteScanJob
    P.ModelQuarantineJournal = ModelQuarantineJournal
    P.set_module_list([ModuleSetting, ModuleScan, ModuleHistory])
except Exception as exc:
    P.logger.error("Exception:%s", str(exc))
    P.logger.error(traceback.format_exc())

logger = P.logger
