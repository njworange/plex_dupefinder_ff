(function (window, $) {
  'use strict';

  function esc(value) {
    return String(value === null || value === undefined ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#039;');
  }

  function notify(message, type) {
    var safe = esc(message || '요청을 처리할 수 없습니다.');
    if ($ && $.notify) $.notify('<strong>' + safe + '</strong>', {type: type || 'info'});
    else window.alert(String(message || '요청을 처리할 수 없습니다.'));
  }

  function request(packageName, moduleName, action, data, method, callback) {
    $.ajax({
      url: '/' + packageName + '/ajax/' + moduleName + '/' + action,
      type: method || 'GET',
      cache: false,
      data: data || {},
      dataType: 'json',
      success: function (ret) {
        if (ret && ret.ret !== 'success' && ret.msg) notify(ret.msg, 'warning');
        if (typeof callback === 'function') callback(ret || {});
      },
      error: function (xhr) {
        var ret = xhr.responseJSON || {};
        notify(ret.msg || '서버 요청이 실패했습니다.', 'danger');
        if (typeof callback === 'function') callback(ret);
      }
    });
  }

  function bytes(value) {
    var size = Number(value || 0);
    if (!isFinite(size) || size <= 0) return '-';
    var units = ['B', 'KB', 'MB', 'GB', 'TB'];
    var index = Math.min(Math.floor(Math.log(size) / Math.log(1024)), units.length - 1);
    return (size / Math.pow(1024, index)).toFixed(index < 2 ? 0 : 2) + ' ' + units[index];
  }

  function duration(value) {
    var seconds = Math.round(Number(value || 0) / 1000);
    if (!seconds) return '-';
    var hours = Math.floor(seconds / 3600);
    var minutes = Math.floor((seconds % 3600) / 60);
    var remain = seconds % 60;
    return (hours ? hours + ':' : '') + String(minutes).padStart(2, '0') + ':' + String(remain).padStart(2, '0');
  }

  function date(value) {
    if (!value) return '-';
    var parsed = new Date(value);
    return isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleString();
  }

  function badge(status) {
    var key = String(status || 'unknown').toLowerCase();
    var kind = 'pdff-secondary';
    if (['completed', 'success', 'safe'].indexOf(key) >= 0) kind = 'pdff-success';
    else if (['running', 'queued'].indexOf(key) >= 0) kind = 'pdff-primary';
    else if (['cancelled', 'cancelling', 'completed_with_warnings', 'unknown'].indexOf(key) >= 0) kind = 'pdff-warning-badge';
    else if (['failed', 'blocked', 'critical', 'verification_failed'].indexOf(key) >= 0) kind = 'pdff-danger-badge';
    return '<span class="pdff-badge ' + kind + '">' + esc(status || '-') + '</span>';
  }

  function flagLabel(flag) {
    var labels = {
      unsupported_media_type: '지원하지 않는 미디어 타입', less_than_two_versions: '버전 2개 미만',
      missing_guid: 'GUID 없음', missing_episode_identity: 'TV 회차 식별정보 없음',
      missing_media_id: 'Media ID 없음', duplicate_media_id: 'Media ID 중복',
      missing_file_path: '파일 경로 없음', multipart_version: '멀티파트 버전',
      shared_file_path: '버전 간 동일 파일 경로', path_outside_allowed_roots: '허용 경로 밖',
      invalid_allowed_root: '허용 루트가 절대 경로가 아님', non_absolute_file_path: '미디어 경로가 절대 경로가 아님',
      missing_machine_id: 'Machine ID 미설정', rescan_required_after_delete: '삭제 후 재스캔 필요',
      delete_outcome_unknown: '삭제 결과 확인 필요', delete_verification_failed: '삭제 대상 재확인 실패',
      delete_postcheck_critical: '유지 버전 재확인 실패', delete_postcheck_media_set_changed: '삭제 후 Media 집합 변경',
      delete_postcheck_snapshot_changed: '삭제 후 남은 Media 정보 변경',
      delete_precheck_blocked: '삭제 전 재검증 차단', restart_delete_validation_interrupted: '재시작으로 삭제 검증 중단',
      restart_delete_outcome_unknown: '재시작 후 삭제 결과 확인 필요'
    };
    return labels[flag] || flag;
  }

  function redact(value, key) {
    if (key && /(token|password|secret|authorization|cookie)/i.test(String(key))) return '[REDACTED]';
    if (Array.isArray(value)) return value.map(function (item) { return redact(item); });
    if (value && typeof value === 'object') {
      var clean = {};
      Object.keys(value).forEach(function (name) { clean[name] = redact(value[name], name); });
      return clean;
    }
    if (typeof value === 'string') {
      return value.replace(/([?&]X-Plex-Token=)[^&\s]+/ig, '$1[REDACTED]');
    }
    return value;
  }

  window.PDFF = {request: request, esc: esc, notify: notify, bytes: bytes, duration: duration, date: date, badge: badge, flagLabel: flagLabel, redact: redact};
})(window, window.jQuery);
