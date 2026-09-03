(function (global) {
  'use strict';

  function valueText(value) {
    if (value === null || value === undefined || value === '') return '-';
    return String(value);
  }

  function number(value) {
    var parsed = Number(value);
    return Number.isFinite(parsed) && parsed >= 0 ? parsed : 0;
  }

  function bytes(value) {
    var amount = number(value);
    var units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
    var unit = 0;
    while (amount >= 1024 && unit < units.length - 1) {
      amount /= 1024;
      unit += 1;
    }
    var digits = unit === 0 || amount >= 100 ? 0 : (amount >= 10 ? 1 : 2);
    return amount.toFixed(digits) + ' ' + units[unit];
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function element(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = valueText(text);
    return node;
  }

  function appendField(parent, label, value, className) {
    var field = element('div', className || 'pdff-field');
    field.appendChild(element('span', 'pdff-label', label));
    field.appendChild(element('span', 'pdff-value', value));
    parent.appendChild(field);
  }

  function statusClass(status) {
    var key = String(status || '').toLowerCase();
    if (key === 'success' || key === 'completed' || key === 'deleted' || key === 'would_delete') return 'pdff-status-success';
    if (key === 'partial' || key === 'stopping' || key === 'stopped' || key === 'interrupted' || key === 'skipped' || key === 'completed_with_errors') return 'pdff-status-warning';
    if (key === 'error' || key === 'failed' || key === 'unknown') return 'pdff-status-danger';
    if (key === 'running' || key === 'deleting' || key === 'queued') return 'pdff-status-running';
    return 'pdff-status-neutral';
  }

  function statusBadge(status) {
    var badge = element('span', 'pdff-status ' + statusClass(status), status || 'unknown');
    return badge;
  }

  function date(value) {
    if (!value) return '-';
    var parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? valueText(value) : parsed.toLocaleString();
  }

  function mode(value) {
    if (value === 'dry_run') return 'Dry Run';
    if (value === 'live') return '즉시 자동 정리';
    return valueText(value);
  }

  function current(value) {
    if (!value) return '-';
    if (typeof value === 'string' || typeof value === 'number') return valueText(value);
    if (typeof value === 'object') {
      return valueText(value.title || value.rating_key || value.file_path || value.id);
    }
    return '-';
  }

  function sidecarItems(value) {
    if (Array.isArray(value)) return value;
    if (typeof value !== 'string' || value.trim() === '') return [];
    try {
      var parsed = JSON.parse(value);
      return Array.isArray(parsed) ? parsed : [];
    } catch (error) {
      return [];
    }
  }

  function renderSidecars(parent, sidecars) {
    var items = sidecarItems(sidecars);
    if (!items.length) return;
    var details = element('details', 'pdff-sidecars');
    details.appendChild(element('summary', '', '외부 자막 ' + items.length + '개'));
    var list = element('ul', 'pdff-sidecar-list');
    items.forEach(function (item) {
      var textValue;
      if (item && typeof item === 'object') {
        var path = item.path || item.file_path || item.source_path || '-';
        var result = item.status || item.result || '';
        textValue = result ? path + ' · ' + result : path;
      } else {
        textValue = item;
      }
      list.appendChild(element('li', 'pdff-path', textValue));
    });
    details.appendChild(list);
    parent.appendChild(details);
  }

  function renderActions(targetId, actions, limit) {
    var target = document.getElementById(targetId);
    if (!target) return;
    clear(target);
    var items = Array.isArray(actions) ? actions : [];
    if (Number.isInteger(limit) && limit >= 0) items = items.slice(0, limit);
    if (!items.length) {
      target.className = 'pdff-empty';
      target.textContent = '최근 작업이 없습니다.';
      return;
    }
    target.className = 'pdff-list';
    items.forEach(function (item) {
      var action = item && typeof item === 'object' ? item : {};
      var card = element('article', 'pdff-card');
      var heading = element('div', 'pdff-card-heading');
      var title = action.title || action.file_path || ('작업 #' + valueText(action.id));
      heading.appendChild(element('strong', 'pdff-card-title', title));
      heading.appendChild(statusBadge(action.status));
      card.appendChild(heading);

      var fields = element('div', 'pdff-card-grid');
      appendField(fields, '모드', mode(action.mode));
      appendField(fields, 'Library', action.section_id);
      appendField(fields, 'Plex ID', action.rating_key);
      appendField(fields, '유지 / 삭제', valueText(action.keep_media_id) + ' / ' + valueText(action.delete_media_id));
      appendField(fields, '점수', valueText(action.keep_score) + ' / ' + valueText(action.delete_score));
      if (action.file_size !== null && action.file_size !== undefined) {
        appendField(fields, '영상 크기', bytes(action.file_size));
      }
      appendField(fields, 'HTTP', action.response_status);
      appendField(fields, '시각', date(action.finished_at || action.created_at));
      card.appendChild(fields);

      if (action.file_path) appendField(card, '영상 경로', action.file_path, 'pdff-field pdff-path');
      if (action.message) appendField(card, '결과', action.message, 'pdff-field');
      renderSidecars(card, action.sidecars);
      target.appendChild(card);
    });
  }

  function renderRuns(targetId, runs) {
    var target = document.getElementById(targetId);
    if (!target) return;
    clear(target);
    var items = Array.isArray(runs) ? runs : [];
    if (!items.length) {
      target.className = 'pdff-empty';
      target.textContent = '최근 실행이 없습니다.';
      return;
    }
    target.className = 'pdff-list';
    items.forEach(function (item) {
      var run = item && typeof item === 'object' ? item : {};
      var progress = run.progress && typeof run.progress === 'object' ? run.progress : {};
      var summary = run.summary && typeof run.summary === 'object' ? run.summary : {};
      var card = element('article', 'pdff-card');
      var heading = element('div', 'pdff-card-heading');
      heading.appendChild(element('strong', 'pdff-card-title', '실행 #' + valueText(run.id) + ' · ' + mode(run.mode)));
      heading.appendChild(statusBadge(run.status));
      card.appendChild(heading);
      var fields = element('div', 'pdff-card-grid');
      appendField(fields, '진행', number(progress.processed) + ' / ' + number(progress.total));
      appendField(fields, '중복 그룹', number(summary.groups));
      appendField(fields, '삭제', number(summary.deleted));
      appendField(fields, '삭제 용량', bytes(summary.bytes));
      appendField(fields, 'Dry Run 대상', number(summary.would_delete));
      appendField(fields, '예상 확보 용량', bytes(summary.would_delete_bytes));
      appendField(fields, '부분 완료 / 오류', number(summary.partial) + ' / ' + number(summary.errors));
      appendField(fields, '시작', date(run.started_at || run.created_at));
      appendField(fields, '종료', date(run.finished_at));
      card.appendChild(fields);
      if (run.current) appendField(card, '현재 항목', current(run.current), 'pdff-field');
      if (run.message) appendField(card, '결과', run.message, 'pdff-field');
      target.appendChild(card);
    });
  }

  function payload(ret) {
    if (ret && typeof ret === 'object' && ret.data && typeof ret.data === 'object') return ret.data;
    return ret && typeof ret === 'object' ? ret : {};
  }

  function ok(ret) {
    return Boolean(ret) && (ret.ret === 'success' || ret.success === true);
  }

  function messageText(ret, fallback) {
    if (ret && typeof ret.msg === 'string' && ret.msg) return ret.msg;
    if (ret && typeof ret.message === 'string' && ret.message) return ret.message;
    return fallback;
  }

  function setText(id, value) {
    var node = document.getElementById(id);
    if (node) node.textContent = valueText(value);
  }

  function setProgress(id, percent) {
    var node = document.getElementById(id);
    if (!node) return;
    var safe = Math.max(0, Math.min(100, number(percent)));
    node.style.width = safe + '%';
    node.setAttribute('aria-valuenow', String(safe));
  }

  function message(id, text, success) {
    var node = document.getElementById(id);
    if (!node) return;
    node.textContent = text || '';
    node.className = text ? 'pdff-message ' + (success ? 'pdff-message-success' : 'pdff-message-error') : 'pdff-message';
  }

  global.PDFF = Object.freeze({
    bytes: bytes,
    current: current,
    date: date,
    message: message,
    messageText: messageText,
    mode: mode,
    number: number,
    ok: ok,
    payload: payload,
    renderActions: renderActions,
    renderRuns: renderRuns,
    setProgress: setProgress,
    setText: setText
  });
}(window));
