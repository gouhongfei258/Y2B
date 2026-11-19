// 主JavaScript文件
(function installCsrfFetchGuard() {
    const originalFetch = window.fetch;
    window.fetch = function(input, init) {
        const options = init ? Object.assign({}, init) : {};
        const method = String(options.method || (input && input.method) || 'GET').toUpperCase();
        if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
            const tokenNode = document.querySelector('meta[name="csrf-token"]');
            const token = tokenNode && tokenNode.getAttribute('content');
            if (token) {
                const headers = new Headers(options.headers || (input && input.headers) || {});
                if (!headers.has('X-CSRF-Token')) headers.set('X-CSRF-Token', token);
                options.headers = headers;
            }
        }
        return originalFetch.call(this, input, options);
    };
})();

document.addEventListener('DOMContentLoaded', function() {
    const csrfMeta = document.querySelector('meta[name="csrf-token"]');
    const csrfValue = csrfMeta && csrfMeta.getAttribute('content');
    if (csrfValue) {
        document.querySelectorAll('form[method="post"], form[method="POST"]').forEach(function(form) {
            if (!form.querySelector('input[name="csrf_token"]')) {
                const input = document.createElement('input');
                input.type = 'hidden';
                input.name = 'csrf_token';
                input.value = csrfValue;
                form.appendChild(input);
            }
        });
    }

    console.log('Y2A-Auto 已加载');

    // --- 设置页面的密码保护逻辑 ---
    const settingsForm = document.querySelector('form[method="post"][enctype="multipart/form-data"]');
    if (settingsForm) {
        const newPassword = document.getElementById('new_password');
        const confirmPassword = document.getElementById('confirm_password');
        const passwordError = document.getElementById('password-match-error');
        const passwordProtectionEnabled = document.getElementById('password_protection_enabled');
        const passwordFields = document.getElementById('password-fields');

        if (passwordProtectionEnabled) {
            function togglePasswordFields() {
                if (passwordProtectionEnabled.checked) {
                    passwordFields.style.display = 'block';
                } else {
                    passwordFields.style.display = 'none';
                }
            }

            // Initial state
            togglePasswordFields();
            passwordProtectionEnabled.addEventListener('change', togglePasswordFields);
        }

        settingsForm.addEventListener('submit', function(event) {
            // 仅在启用密码保护时才校验密码匹配
            if (passwordProtectionEnabled && passwordProtectionEnabled.checked &&
                newPassword && confirmPassword && newPassword.value !== confirmPassword.value) {
                event.preventDefault(); // 阻止表单提交
                if (passwordError) {
                    passwordError.classList.remove('d-none');
                }
            } else {
                if (passwordError) {
                    passwordError.classList.add('d-none');
                }
            }
        });
    }


    // --- 设置页面的日志清理按钮逻辑 ---
    // 绑定手动日志清理按钮
    const manualCleanupBtn = document.getElementById('manual-cleanup-btn');
    const logCleanupHoursField = document.getElementById('log-cleanup-hours');
    const cleanupHoursHidden = document.getElementById('cleanup-hours-input');
    if(manualCleanupBtn) {
        manualCleanupBtn.addEventListener('click', function() {
            // 使用当前输入的小时数（若存在）
            if (logCleanupHoursField && cleanupHoursHidden) {
                const hours = parseInt(logCleanupHoursField.value, 10);
                if (!isNaN(hours) && hours > 0) {
                    cleanupHoursHidden.value = hours;
                }
            }
            const confirmMsg = `确定要手动清理旧日志吗？将删除 ${cleanupHoursHidden ? cleanupHoursHidden.value : ''} 小时前的日志文件。`;
            if (confirm(confirmMsg)) {
                const form = document.getElementById('cleanup-form');
                if (form && window.y2bSubmitForm) window.y2bSubmitForm(form);
            }
        });
    }

    // 绑定立即清空日志按钮
    const clearLogsBtn = document.getElementById('clear-logs-btn');
    const confirmClearBtn = document.getElementById('confirm-clear-btn');
    const cancelClearBtn = document.getElementById('cancel-clear-btn');
    const clearWarning = document.getElementById('clear-warning');

    if (clearLogsBtn) {
        clearLogsBtn.addEventListener('click', function() {
            clearLogsBtn.classList.add('d-none');
            confirmClearBtn.classList.remove('d-none');
            cancelClearBtn.classList.remove('d-none');
            clearWarning.classList.remove('d-none');
        });
    }

    if (cancelClearBtn) {
        cancelClearBtn.addEventListener('click', function() {
            clearLogsBtn.classList.remove('d-none');
            confirmClearBtn.classList.add('d-none');
            cancelClearBtn.classList.add('d-none');
            clearWarning.classList.add('d-none');
        });
    }
    
    if (confirmClearBtn) {
        confirmClearBtn.addEventListener('click', function() {
             const clearForm = document.getElementById('clear-form');
             if (clearForm && window.y2bSubmitForm) window.y2bSubmitForm(clearForm);
        });
    }

    // --- 设置页面的下载内容清理按钮逻辑 ---
    // 绑定手动下载内容清理按钮
    const manualDownloadCleanupBtn = document.getElementById('manual-download-cleanup-btn');
    const downloadCleanupHoursField = document.getElementById('download-cleanup-hours');
    const downloadCleanupHoursHidden = document.getElementById('download-cleanup-hours-input');
    if(manualDownloadCleanupBtn) {
        manualDownloadCleanupBtn.addEventListener('click', function() {
            // 使用当前输入的小时数（若存在）
            if (downloadCleanupHoursField && downloadCleanupHoursHidden) {
                const hours = parseInt(downloadCleanupHoursField.value, 10);
                if (!isNaN(hours) && hours > 0) {
                    downloadCleanupHoursHidden.value = hours;
                }
            }
            const confirmMsg = `确定要手动清理旧的下载内容吗？将删除 ${downloadCleanupHoursHidden ? downloadCleanupHoursHidden.value : ''} 小时前的下载文件和目录。`;
            if (confirm(confirmMsg)) {
                const form = document.getElementById('download-cleanup-form');
                if (form && window.y2bSubmitForm) window.y2bSubmitForm(form);
            }
        });
    }
});

(function installSamePageActions() {
    function toastTone(category) {
        if (category === 'danger' || category === 'error') return 'danger';
        if (category === 'warning') return 'warning';
        if (category === 'success') return 'success';
        if (category === 'info') return 'info';
        return 'secondary';
    }

    function ensureToastContainer() {
        let container = document.getElementById('app-toast-container');
        if (container) return container;
        container = document.createElement('div');
        container.id = 'app-toast-container';
        container.className = 'toast-container position-fixed top-0 end-0 p-3 app-toast-container';
        document.body.appendChild(container);
        return container;
    }

    function showToast(category, message) {
        const text = String(message || '').trim();
        if (!text) return;
        const tone = toastTone(category);
        if (typeof bootstrap === 'undefined' || !bootstrap.Toast) return;
        const container = ensureToastContainer();
        const toastEl = document.createElement('div');
        toastEl.className = 'toast align-items-center text-bg-' + tone + ' border-0';
        toastEl.setAttribute('role', 'alert');
        toastEl.setAttribute('aria-live', 'assertive');
        toastEl.setAttribute('aria-atomic', 'true');
        const wrapper = document.createElement('div');
        wrapper.className = 'd-flex';
        const body = document.createElement('div');
        body.className = 'toast-body';
        body.textContent = text;
        const closeBtn = document.createElement('button');
        closeBtn.type = 'button';
        closeBtn.className = (tone === 'warning' || tone === 'info')
            ? 'btn-close me-2 m-auto'
            : 'btn-close btn-close-white me-2 m-auto';
        closeBtn.setAttribute('data-bs-dismiss', 'toast');
        closeBtn.setAttribute('aria-label', '关闭');
        wrapper.appendChild(body);
        wrapper.appendChild(closeBtn);
        toastEl.appendChild(wrapper);
        container.appendChild(toastEl);
        toastEl.addEventListener('hidden.bs.toast', function () {
            toastEl.remove();
        });
        bootstrap.Toast.getOrCreateInstance(toastEl, { delay: 4500 }).show();
    }

    async function refreshRegions(selectors) {
        const list = (selectors || []).filter(Boolean);
        if (!list.length) return 0;
        const response = await fetch(window.location.href, {
            method: 'GET',
            cache: 'no-store',
            headers: {
                'X-Requested-With': 'XMLHttpRequest',
                'Cache-Control': 'no-cache, no-store, max-age=0',
                'Pragma': 'no-cache'
            }
        });
        if (!response.ok) {
            throw new Error('页面局部刷新失败');
        }
        const html = await response.text();
        const doc = new DOMParser().parseFromString(html, 'text/html');
        let replaced = 0;
        list.forEach(function (selector) {
            const next = doc.querySelector(selector);
            const current = document.querySelector(selector);
            if (!next || !current) return;
            current.replaceWith(document.importNode(next, true));
            replaced += 1;
        });
        if (typeof window.bindMonitorRunForms === 'function') {
            window.bindMonitorRunForms();
        }
        return replaced;
    }

    function closeModal(modalId) {
        if (!modalId || typeof bootstrap === 'undefined' || !bootstrap.Modal) return;
        const modalEl = document.getElementById(modalId);
        if (!modalEl) return;
        const modal = bootstrap.Modal.getInstance(modalEl) || bootstrap.Modal.getOrCreateInstance(modalEl);
        modal.hide();
    }

    function applyResult(form, data) {
        if (data && data.redirect) {
            window.location.assign(data.redirect);
            return;
        }
        const category = data && data.category
            ? data.category
            : (data && data.success === false ? 'danger' : 'success');
        showToast(category, data && data.message);
        if (data && data.success && form) {
            const modalId = form.getAttribute('data-close-modal');
            if (modalId) closeModal(modalId);
        }
        const mode = form ? (form.getAttribute('data-same-page') || '') : '';
        const handlers = window.y2bSamePageHandlers || {};
        if (mode && typeof handlers[mode] === 'function') {
            handlers[mode](form, data || {});
            return;
        }
        if (mode === 'refresh-tasks' && typeof window.refreshTasksData === 'function') {
            window.refreshTasksData(true);
            return;
        }
        if (mode === 'refresh-region' && form) {
            const selectors = (form.getAttribute('data-refresh-root') || '')
                .split(',')
                .map(function (item) { return item.trim(); })
                .filter(Boolean);
            refreshRegions(selectors).catch(function (error) {
                showToast('danger', error && error.message ? error.message : '局部刷新失败');
            });
        }
    }

    function buildFormData(form, submitter) {
        if (submitter && typeof FormData === 'function') {
            try {
                return new FormData(form, submitter);
            } catch (error) {
                const fallback = new FormData(form);
                if (submitter.name) fallback.append(submitter.name, submitter.value);
                return fallback;
            }
        }
        return new FormData(form);
    }

    async function submitForm(form, submitter) {
        const button = submitter || form.querySelector('button[type="submit"], input[type="submit"]');
        const formData = buildFormData(form, button);
        if (button) button.disabled = true;
        try {
            const response = await fetch(form.action || window.location.href, {
                method: 'POST',
                body: formData,
                headers: {
                    'X-Requested-With': 'XMLHttpRequest',
                    'Accept': 'application/json'
                }
            });
            if (response.redirected && response.url && /\/login(?:\?|$)/.test(response.url)) {
                window.location.assign(response.url);
                return null;
            }
            let data = null;
            try {
                data = await response.json();
            } catch (parseError) {
                throw new Error('操作失败，请刷新页面后重试');
            }
            applyResult(form, data || {});
            return data;
        } finally {
            if (button && document.body.contains(button)) {
                button.disabled = false;
            }
        }
    }

    document.addEventListener('submit', function (event) {
        const form = event.target;
        if (!form || !form.getAttribute || !form.hasAttribute('data-same-page')) return;
        if (form.getAttribute('data-same-page') === 'off') return;
        if (form.classList && form.classList.contains('monitor-run-form')) return;
        if (event.defaultPrevented) return;
        event.preventDefault();
        submitForm(form, event.submitter).catch(function (error) {
            showToast('danger', error && error.message ? error.message : '操作失败，请重试');
        });
    });

    window.y2bShowToast = showToast;
    window.y2bSubmitForm = function (form, submitter) {
        return submitForm(form, submitter).catch(function (error) {
            showToast('danger', error && error.message ? error.message : '操作失败，请重试');
        });
    };
    window.y2bRefreshRegions = refreshRegions;
})(); 