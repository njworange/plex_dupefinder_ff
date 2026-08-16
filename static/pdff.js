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
    if (['completed', 'success', 'succeeded', 'safe', 'quarantined'].indexOf(key) >= 0) kind = 'pdff-success';
    else if (['running', 'executing', 'approved', 'queued'].indexOf(key) >= 0) kind = 'pdff-primary';
    else if (['planned', 'preview', 'ready', 'draft', 'pending', 'skipped'].indexOf(key) >= 0) kind = 'pdff-secondary';
    else if (['cancelled', 'cancelling', 'completed_with_warnings', 'completed_with_errors', 'unknown', 'stopped', 'interrupted', 'expired', 'retry_wait', 'unverified'].indexOf(key) >= 0) kind = 'pdff-warning-badge';
    else if (['failed', 'blocked', 'critical', 'verification_failed', 'recovery_required'].indexOf(key) >= 0) kind = 'pdff-danger-badge';
    return '<span class="pdff-badge ' + kind + '">' + esc(status || '-') + '</span>';
  }

  function deleteBudget(value) {
    value = value && typeof value === 'object' ? value : {};
    var nested = value.delete_budget;
    var budget = nested && typeof nested === 'object' ? nested : value;
    var limitRaw = budget.limit;
    var limit = Number(limitRaw);
    // A malformed/missing runtime payload must never make the UI less strict.
    if ((typeof limitRaw !== 'number' && typeof limitRaw !== 'string') ||
        String(limitRaw).trim() === '' || !Number.isInteger(limit) ||
        limit < 1 || limit > 100) limit = 1;
    var attemptedRaw = budget.attempted !== undefined
      ? budget.attempted : value.deletion_attempts;
    var attempted = Number(attemptedRaw);
    if ((typeof attemptedRaw !== 'number' && typeof attemptedRaw !== 'string') ||
        String(attemptedRaw).trim() === '' || !Number.isInteger(attempted) ||
        attempted < 0) attempted = limit;
    var remaining = Math.max(0, limit - attempted);
    return {
      limit: limit,
      attempted: attempted,
      remaining: remaining,
      exhausted: remaining === 0
    };
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

  function subtitleCleanup(value) {
    value = value && typeof value === 'object' ? value : {};
    var cleanup = value.subtitle_cleanup && typeof value.subtitle_cleanup === 'object'
      ? value.subtitle_cleanup : value;
    var eligible = cleanup.eligible || cleanup.included_subtitles || [];
    var excluded = cleanup.excluded || cleanup.excluded_subtitles || [];
    if (!Array.isArray(eligible)) eligible = [];
    if (!Array.isArray(excluded)) excluded = [];
    var counts = cleanup.counts && typeof cleanup.counts === 'object' ? cleanup.counts : {};
    var backend = String(cleanup.backend || value.backend || '');
    var detailsPresent = ['eligible', 'excluded', 'included_subtitles', 'excluded_subtitles'].some(function (key) {
      return Object.prototype.hasOwnProperty.call(cleanup, key);
    });
    return {
      present: Boolean(value.subtitle_cleanup || cleanup.included_subtitles || cleanup.excluded_subtitles || cleanup.eligible || cleanup.excluded || backend),
      detailsPresent: detailsPresent,
      enabled: cleanup.enabled === true || backend === 'quarantine',
      backend: backend || 'plex',
      status: String(cleanup.status || (backend === 'quarantine' ? 'planned' : 'disabled')),
      planDigest: String(cleanup.plan_digest || value.plan_digest || ''),
      quarantineDir: String(cleanup.quarantine_dir || cleanup.quarantine_root || ''),
      eligible: eligible,
      excluded: excluded,
      eligibleCount: Number(counts.eligible !== undefined ? counts.eligible : eligible.length) || 0,
      excludedCount: Number(counts.excluded !== undefined ? counts.excluded : excluded.length) || 0,
      quarantinedCount: Number(counts.quarantined || 0) || 0
    };
  }

  function subtitlePath(entry, eligible) {
    if (typeof entry === 'string') return entry;
    entry = entry && typeof entry === 'object' ? entry : {};
    return String(eligible
      ? (entry.source_path || entry.path || entry.file || '')
      : (entry.path || entry.source_path || entry.file || ''));
  }

  function subtitleReason(entry, fallback) {
    var value = entry && typeof entry === 'object' && entry.reason
      ? String(entry.reason) : '';
    var labels = {
      exclusive_to_deleted_video: '삭제 영상에만 정확히 대응하는 일반 외부 자막',
      ambiguous_owner: '같은 파일명을 쓰는 유지본이 있어 소유권이 모호함',
      shared_with_surviving_or_sibling_video: '유지본 또는 다른 영상과 공유될 수 있음',
      survivor_owned: '유지할 영상에 대응하는 자막',
      subtitle_name_not_exclusive: '파일명만으로 삭제 영상 전용임을 증명할 수 없음',
      unsupported_or_paired_subtitle_format: '쌍 파일 또는 현재 안전 처리 대상이 아닌 자막 형식',
      symlink: '심볼릭 링크 또는 reparse 경로',
      symlink_or_reparse_not_safe: '심볼릭 링크 또는 reparse 경로',
      hardlink: '하드링크라 다른 경로와 파일 내용을 공유함',
      hardlink_not_safe: '하드링크라 다른 경로와 파일 내용을 공유함',
      subtitle_too_large: '자막 안전 크기 한도를 초과함',
      not_regular_file: '일반 파일이 아님',
      file_state_unverifiable: '파일 identity를 안전하게 확인할 수 없음',
      different_filesystem: '영상과 다른 파일시스템이라 원자 격리를 보장할 수 없음',
      subtitle_directory_reparse_point: '자막 폴더가 링크 또는 reparse 경로임',
      protected_for_surviving_video: '유지할 영상의 보호 대상 자막'
    };
    return labels[value] || value || fallback;
  }

  function subtitleCleanupHtml(value, phase) {
    var cleanup = subtitleCleanup(value);
    var isResult = phase === 'result' || cleanup.status === 'quarantined' || cleanup.status === 'recovery_required';
    var html = '<div class="pdff-subtitle-summary"><strong>외부 자막 안전 처리</strong> ' +
      badge(cleanup.status) + '<span class="pdff-muted ml-2">방식 ' + esc(cleanup.backend) +
      ' · 함께 격리 ' + esc(isResult ? cleanup.quarantinedCount : cleanup.eligibleCount) +
      ' · 위험 예외 ' + esc(cleanup.excludedCount) + '</span></div>';
    if (cleanup.backend !== 'quarantine' || !cleanup.enabled) {
      return html + '<div class="pdff-danger mt-2">Plex Media DELETE 방식에서는 자막을 직접 선별·격리하지 않으며, PMS가 외부 자막을 어떻게 처리할지 이 플러그인이 보장할 수 없습니다.</div>';
    }
    html += '<div class="pdff-muted mt-2">영구삭제가 아니라 격리 이동입니다. 승인한 목록과 파일 상태를 실행 직전에 정확히 재검증하며, 달라지면 아무 파일도 이동하지 않고 새 사전확인을 요구합니다. 모호한 자막은 이동하지 않습니다.</div>';
    if (cleanup.quarantineDir) {
      html += '<div class="mt-2"><span class="pdff-kv-label">격리 위치</span><div class="pdff-code">' + esc(cleanup.quarantineDir) + '</div></div>';
    }
    if (cleanup.eligible.length) {
      html += '<details class="pdff-subtitle-details mt-2" open><summary>함께 격리 ' + esc(cleanup.eligible.length) + '개</summary>';
      cleanup.eligible.forEach(function (entry) {
        var destination = entry && typeof entry === 'object'
          ? String(entry.quarantine_path || entry.destination_path || '') : '';
        html += '<div class="pdff-subtitle-entry pdff-subtitle-eligible"><div class="pdff-code">' + esc(subtitlePath(entry, true)) + '</div>' +
          (destination ? '<div class="pdff-muted">→ ' + esc(destination) + '</div>' : '') +
          '<div class="small">' + esc(subtitleReason(entry, '삭제 영상에만 대응하는 외부 자막')) + '</div></div>';
      });
      html += '</details>';
    } else {
      html += '<div class="pdff-muted mt-2">함께 격리할 전용 외부 자막이 없습니다.</div>';
    }
    if (cleanup.excluded.length) {
      html += '<details class="pdff-subtitle-details pdff-subtitle-exceptions mt-2" open><summary>위험·모호하여 제외 ' + esc(cleanup.excluded.length) + '개</summary>';
      cleanup.excluded.forEach(function (entry) {
        html += '<div class="pdff-subtitle-entry pdff-subtitle-excluded"><div class="pdff-code">' + esc(subtitlePath(entry, false)) + '</div>' +
          '<div class="small"><strong>보존 사유:</strong> ' + esc(subtitleReason(entry, '유지본과의 관계를 안전하게 확정할 수 없음')) + '</div></div>';
      });
      html += '</details>';
    }
    if (cleanup.status === 'recovery_required') {
      html += '<div class="pdff-danger mt-2"><strong>복구 확인 필요:</strong> 격리 이동 결과가 완결되지 않았습니다. 감사 상세와 실제 파일을 확인하세요.</div>';
    }
    return html;
  }

  window.PDFF = {request: request, esc: esc, notify: notify, bytes: bytes, duration: duration, date: date, badge: badge, deleteBudget: deleteBudget, flagLabel: flagLabel, redact: redact, subtitleCleanup: subtitleCleanup, subtitleCleanupHtml: subtitleCleanupHtml};
})(window, window.jQuery);
