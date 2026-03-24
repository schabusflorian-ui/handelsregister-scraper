/**
 * Handelsregister Startup Discovery Platform - Frontend JS
 *
 * Shared utilities: toast notifications, loading states, keyboard navigation.
 * Extracted from inline scripts in companies.html and company_detail.html.
 */

// ============================================================
// Toast Notification System
// ============================================================

function showToast(message, type = 'success', duration = 3000) {
    let container = document.getElementById('toast-container');
    if (!container) {
        container = document.createElement('div');
        container.id = 'toast-container';
        container.className = 'fixed bottom-4 right-4 z-50 flex flex-col gap-2';
        document.body.appendChild(container);
    }
    const colors = {
        success: 'bg-green-600',
        error: 'bg-red-600',
        info: 'bg-indigo-600',
        warning: 'bg-yellow-600',
    };
    const toast = document.createElement('div');
    toast.className = `${colors[type] || colors.info} text-white px-4 py-2.5 rounded-lg shadow-lg text-sm font-medium transform transition-all duration-300 translate-y-2 opacity-0`;
    toast.textContent = message;
    container.appendChild(toast);
    requestAnimationFrame(() => {
        toast.classList.remove('translate-y-2', 'opacity-0');
    });
    setTimeout(() => {
        toast.classList.add('translate-y-2', 'opacity-0');
        setTimeout(() => toast.remove(), 300);
    }, duration);
}


// ============================================================
// Loading Overlay for Tables
// ============================================================

function showTableLoading() {
    const table = document.getElementById('companies-table');
    if (!table) return;
    const container = table.closest('.overflow-x-auto');
    if (!container) return;
    container.style.position = 'relative';
    const overlay = document.createElement('div');
    overlay.id = 'table-loading-overlay';
    overlay.className = 'absolute inset-0 bg-white/60 flex items-center justify-center z-10';
    overlay.innerHTML = '<div class="animate-spin h-8 w-8 border-4 border-indigo-600 border-t-transparent rounded-full"></div>';
    container.appendChild(overlay);
}

// Intercept pagination/sort clicks to show loading
document.addEventListener('click', function(e) {
    const link = e.target.closest('a[href*="page="], a[href*="sort="], a[href*="per_page="]');
    if (link && link.closest('#companies-table, .pagination-controls')) {
        showTableLoading();
    }
});

// Intercept form submits on companies filter form
document.addEventListener('submit', function(e) {
    if (e.target.closest('form[action="/companies"]')) {
        showTableLoading();
    }
});


// ============================================================
// Companies Page - Column Visibility
// ============================================================

const COLUMNS = {
    relevance:      { label: 'Relevance',      defaultVisible: true },
    company:        { label: 'Company',        defaultVisible: true },
    legal_form:     { label: 'Legal Form',     defaultVisible: true },
    city:           { label: 'City',           defaultVisible: true },
    state:          { label: 'State',          defaultVisible: true },
    year:           { label: 'Year',           defaultVisible: true },
    website:        { label: 'Website',        defaultVisible: true },
    ai_score:       { label: 'AI Score',       defaultVisible: true },
    climate_score:  { label: 'Climate Score',  defaultVisible: true },
    status:         { label: 'Status',         defaultVisible: true },
    classification: { label: 'Classification', defaultVisible: false },
    startup_score:  { label: 'Startup Score',  defaultVisible: false },
    capital:        { label: 'Capital',        defaultVisible: false },
    registry:       { label: 'Registry Court', defaultVisible: false },
    reg_date:       { label: 'Reg. Date',      defaultVisible: false },
    purpose:        { label: 'Purpose',        defaultVisible: false },
    source:         { label: 'Source',          defaultVisible: false },
    keywords:       { label: 'Keywords',       defaultVisible: false },
};

const COL_STORAGE_KEY = 'hr_visible_columns';

function getVisibleCols() {
    try {
        const stored = localStorage.getItem(COL_STORAGE_KEY);
        if (stored) return JSON.parse(stored);
    } catch (e) {}
    return Object.entries(COLUMNS).filter(([_, c]) => c.defaultVisible).map(([k]) => k);
}

function saveVisibleCols(cols) {
    localStorage.setItem(COL_STORAGE_KEY, JSON.stringify(cols));
}

function applyColumns() {
    const visible = new Set(getVisibleCols());
    document.querySelectorAll('[data-col]').forEach(el => {
        el.style.display = visible.has(el.dataset.col) ? '' : 'none';
    });
    document.querySelectorAll('#col-checkboxes input[type=checkbox]').forEach(cb => {
        cb.checked = visible.has(cb.value);
    });
    const countEl = document.getElementById('col-count');
    if (countEl) {
        countEl.textContent = visible.size + ' of ' + Object.keys(COLUMNS).length + ' columns';
    }
}

function toggleCol(colKey) {
    const cols = getVisibleCols();
    const idx = cols.indexOf(colKey);
    if (idx >= 0) {
        if (cols.length <= 1) return;
        cols.splice(idx, 1);
    } else {
        cols.push(colKey);
    }
    saveVisibleCols(cols);
    applyColumns();
}

function colSelectAll() {
    saveVisibleCols(Object.keys(COLUMNS));
    applyColumns();
}

function colSelectDefaults() {
    saveVisibleCols(Object.entries(COLUMNS).filter(([_, c]) => c.defaultVisible).map(([k]) => k));
    applyColumns();
}

function toggleColMenu() {
    document.getElementById('col-menu').classList.toggle('hidden');
}

function initColumnSelector() {
    const container = document.getElementById('col-checkboxes');
    if (!container) return;
    for (const [key, cfg] of Object.entries(COLUMNS)) {
        const label = document.createElement('label');
        label.className = 'flex items-center px-3 py-1 hover:bg-gray-50 cursor-pointer text-sm text-gray-700';
        label.innerHTML = '<input type="checkbox" value="' + key + '" class="rounded border-gray-300 text-indigo-600 mr-2" onchange="toggleCol(\'' + key + '\')"> ' + cfg.label;
        container.appendChild(label);
    }
    applyColumns();

    // Close menu on outside click
    document.addEventListener('click', function(e) {
        const selector = document.getElementById('col-selector');
        if (selector && !selector.contains(e.target)) {
            document.getElementById('col-menu').classList.add('hidden');
        }
    });
}


// ============================================================
// Companies Page - Relevance Management
// ============================================================

async function setRelevance(companyId, value, selectEl) {
    try {
        const resp = await fetch('/companies/' + companyId + '/relevance', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({relevance: value || null}),
        });
        if (!resp.ok) throw new Error('Failed');
        if (selectEl) applyRelevanceStyle(selectEl, value);
        showToast(value ? 'Marked as ' + value : 'Relevance cleared', 'success');
    } catch (e) {
        showToast('Error updating relevance', 'error');
    }
}

function applyRelevanceStyle(select, value) {
    select.className = select.className.replace(/bg-\S+|text-\S+|ring-\S+/g, '');
    select.className += ' rounded-md border-gray-300 text-xs py-1 px-1.5 ';
    if (value === 'relevant') {
        select.className += 'bg-green-50 text-green-800 ring-1 ring-green-300';
    } else if (value === 'irrelevant') {
        select.className += 'bg-red-50 text-red-800 ring-1 ring-red-300';
    } else {
        select.className += 'bg-gray-50 text-gray-500';
    }
}


// ============================================================
// Companies Page - Bulk Selection
// ============================================================

function getSelectedIds() {
    return Array.from(document.querySelectorAll('.row-select:checked')).map(cb => parseInt(cb.value));
}

function updateBulkBar() {
    const ids = getSelectedIds();
    const bar = document.getElementById('bulk-bar');
    const count = document.getElementById('bulk-count');
    if (!bar) return;
    if (ids.length > 0) {
        bar.classList.remove('hidden');
        count.textContent = ids.length;
    } else {
        bar.classList.add('hidden');
    }
    const allBoxes = document.querySelectorAll('.row-select');
    const allChecked = allBoxes.length > 0 && ids.length === allBoxes.length;
    const selectAll = document.getElementById('select-all');
    if (selectAll) {
        selectAll.checked = allChecked;
        selectAll.indeterminate = ids.length > 0 && !allChecked;
    }
}

function toggleSelectAll(checked) {
    document.querySelectorAll('.row-select').forEach(cb => cb.checked = checked);
    updateBulkBar();
}

function clearSelection() {
    toggleSelectAll(false);
}

async function bulkSetRelevance(value) {
    const ids = getSelectedIds();
    if (!ids.length) return;
    try {
        const resp = await fetch('/companies/bulk-relevance', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({company_ids: ids, relevance: value}),
        });
        if (!resp.ok) throw new Error('Failed');
        ids.forEach(id => {
            const row = document.querySelector('tr[data-company-id="' + id + '"]');
            if (!row) return;
            const select = row.querySelector('[data-col="relevance"] select');
            if (select) {
                select.value = value || '';
                applyRelevanceStyle(select, value);
            }
        });
        clearSelection();
        showToast(ids.length + ' companies updated', 'success');
    } catch (e) {
        showToast('Error updating relevance', 'error');
    }
}


// ============================================================
// Companies Page - Filter Presets
// ============================================================

function savePreset(currentFilterQs) {
    if (!currentFilterQs || !currentFilterQs.trim()) {
        showToast('No active filters to save. Apply some filters first.', 'warning');
        return;
    }
    const name = prompt('Name this filter preset:');
    if (!name) return;
    fetch('/api/filter-presets', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name: name, params: currentFilterQs}),
    }).then(r => {
        if (r.ok) { showToast('Filter preset saved', 'success'); setTimeout(() => location.reload(), 500); }
        else showToast('Error saving preset', 'error');
    });
}

function deletePreset(id, name) {
    if (!confirm('Delete preset "' + name + '"?')) return;
    fetch('/api/filter-presets/' + id, {method: 'DELETE'})
        .then(r => {
            if (r.ok) { showToast('Preset deleted', 'success'); setTimeout(() => location.reload(), 500); }
            else showToast('Error deleting preset', 'error');
        });
}


// ============================================================
// Company Detail - Contacted Toggle
// ============================================================

async function toggleContacted(companyId) {
    try {
        const resp = await fetch('/companies/' + companyId + '/toggle-contacted', { method: 'POST' });
        const data = await resp.json();

        if (data.success) {
            const btn = document.getElementById('contacted-btn');
            const text = document.getElementById('contacted-text');

            if (data.contacted) {
                btn.className = btn.className
                    .replace('bg-white text-gray-700 ring-1 ring-inset ring-gray-300 hover:bg-gray-50',
                             'bg-green-600 text-white hover:bg-green-500');
                text.textContent = 'Contacted';
                showToast('Marked as contacted', 'success');
            } else {
                btn.className = btn.className
                    .replace('bg-green-600 text-white hover:bg-green-500',
                             'bg-white text-gray-700 ring-1 ring-inset ring-gray-300 hover:bg-gray-50');
                text.textContent = 'Mark Contacted';
                showToast('Removed contacted status', 'info');
            }
        }
    } catch (e) {
        showToast('Failed to toggle contacted', 'error');
    }
}


// ============================================================
// Company Detail - Notes
// ============================================================

async function saveNotes(companyId) {
    const notes = document.getElementById('notes').value;
    try {
        const resp = await fetch('/companies/' + companyId + '/notes', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ notes }),
        });
        const data = await resp.json();
        if (data.success) {
            showToast('Notes saved', 'success');
        }
    } catch (e) {
        showToast('Failed to save notes', 'error');
    }
}


// ============================================================
// Companies Page - Keyboard Navigation + Quick View
// ============================================================

let focusedRowIndex = -1;
let expandedRowId = null;

function isInputFocused() {
    const tag = document.activeElement?.tagName?.toLowerCase();
    return tag === 'input' || tag === 'textarea' || tag === 'select';
}

function getCompanyRows() {
    return Array.from(document.querySelectorAll('#companies-table tbody tr[data-company-id]'));
}

function setFocusedRow(index) {
    const rows = getCompanyRows();
    if (rows.length === 0) return;

    // Clear previous focus
    rows.forEach(r => r.classList.remove('bg-indigo-50', 'ring-1', 'ring-indigo-200'));

    // Clamp index
    if (index < 0) index = 0;
    if (index >= rows.length) index = rows.length - 1;
    focusedRowIndex = index;

    // Apply focus
    const row = rows[index];
    row.classList.add('bg-indigo-50', 'ring-1', 'ring-indigo-200');

    // Scroll into view
    row.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
}

async function toggleQuickView(companyId, row) {
    const existingDetail = document.getElementById('quick-view-' + companyId);

    if (existingDetail) {
        // Collapse
        existingDetail.remove();
        expandedRowId = null;
        return;
    }

    // Collapse any other expanded row
    document.querySelectorAll('[id^="quick-view-"]').forEach(el => el.remove());

    // Fetch quick-view partial
    try {
        const resp = await fetch('/api/companies/' + companyId + '/quick-view');
        if (!resp.ok) throw new Error('Failed');
        const html = await resp.text();

        // Insert a new row below the current one
        const detailRow = document.createElement('tr');
        detailRow.id = 'quick-view-' + companyId;
        detailRow.innerHTML = '<td colspan="20" class="p-0">' + html + '</td>';
        row.after(detailRow);
        expandedRowId = companyId;

        // Mark as viewed (remove "New" badge from status cell)
        const statusCell = row.querySelector('[data-col="status"]');
        if (statusCell) {
            const newBadge = statusCell.querySelector('.bg-yellow-100');
            if (newBadge) {
                newBadge.innerHTML = '<svg class="w-3 h-3" fill="currentColor" viewBox="0 0 20 20"><path d="M10 12a2 2 0 100-4 2 2 0 000 4z"/><path fill-rule="evenodd" d="M.458 10C1.732 5.943 5.522 3 10 3s8.268 2.943 9.542 7c-1.274 4.057-5.064 7-9.542 7S1.732 14.057.458 10zM14 10a4 4 0 11-8 0 4 4 0 018 0z" clip-rule="evenodd"/></svg>';
                newBadge.className = 'inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium bg-blue-100 text-blue-800';
            }
        }
    } catch (e) {
        showToast('Failed to load company details', 'error');
    }
}

function initKeyboardNav() {
    if (!document.getElementById('companies-table')) return;

    document.addEventListener('keydown', function(e) {
        if (isInputFocused()) return;

        const rows = getCompanyRows();
        if (rows.length === 0) return;

        switch (e.key) {
            case 'j': // Move down
                e.preventDefault();
                setFocusedRow(focusedRowIndex + 1);
                break;

            case 'k': // Move up
                e.preventDefault();
                setFocusedRow(focusedRowIndex - 1);
                break;

            case 'Enter':
            case ' ': // Toggle quick-view
                e.preventDefault();
                if (focusedRowIndex >= 0 && focusedRowIndex < rows.length) {
                    const row = rows[focusedRowIndex];
                    const companyId = row.dataset.companyId;
                    toggleQuickView(companyId, row);
                }
                break;

            case 'r': // Cycle relevance
                e.preventDefault();
                if (focusedRowIndex >= 0 && focusedRowIndex < rows.length) {
                    const row = rows[focusedRowIndex];
                    const select = row.querySelector('[data-col="relevance"] select');
                    if (select) {
                        const cycle = ['', 'relevant', 'irrelevant'];
                        const currentIdx = cycle.indexOf(select.value);
                        const nextVal = cycle[(currentIdx + 1) % cycle.length];
                        select.value = nextVal;
                        setRelevance(row.dataset.companyId, nextVal, select);
                    }
                }
                break;

            case 'c': // Toggle contacted
                e.preventDefault();
                if (focusedRowIndex >= 0 && focusedRowIndex < rows.length) {
                    const row = rows[focusedRowIndex];
                    toggleContacted(row.dataset.companyId);
                }
                break;

            case 'o': // Open in new tab
                e.preventDefault();
                if (focusedRowIndex >= 0 && focusedRowIndex < rows.length) {
                    const row = rows[focusedRowIndex];
                    window.open('/companies/' + row.dataset.companyId, '_blank');
                }
                break;

            case 'Escape': // Collapse quick-view
                document.querySelectorAll('[id^="quick-view-"]').forEach(el => el.remove());
                expandedRowId = null;
                break;
        }
    });
}


// ============================================================
// Stealth Job Admin - Controls
// ============================================================

let refreshInterval = null;

async function runStealthJob() {
    const btn = document.getElementById('run-btn');
    if (!btn) return;
    btn.disabled = true;
    btn.innerHTML = '<svg class="animate-spin mr-2 h-4 w-4" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path></svg> Running...';

    try {
        const resp = await fetch('/admin/stealth-job/run', { method: 'POST' });
        const data = await resp.json();
        if (data.error) {
            showToast('Error: ' + data.error, 'error');
        } else {
            startStatusPolling();
            showToast('Job started', 'success');
        }
    } catch (e) {
        showToast('Failed to start job', 'error');
        btn.disabled = false;
        btn.innerHTML = 'Run Now (Cloud)';
    }
}

async function scheduleStealthJob() {
    const hours = document.getElementById('interval').value;
    try {
        const resp = await fetch('/admin/stealth-job/schedule?hours=' + hours, { method: 'POST' });
        const data = await resp.json();
        showToast(data.message || data.error, data.error ? 'error' : 'success');
        setTimeout(() => location.reload(), 500);
    } catch (e) {
        showToast('Failed: ' + e, 'error');
    }
}

async function stopStealthSchedule() {
    try {
        const resp = await fetch('/admin/stealth-job/stop', { method: 'POST' });
        const data = await resp.json();
        showToast(data.message || data.error, data.error ? 'error' : 'success');
        setTimeout(() => location.reload(), 500);
    } catch (e) {
        showToast('Failed: ' + e, 'error');
    }
}

function startStatusPolling() {
    if (refreshInterval) clearInterval(refreshInterval);
    refreshInterval = setInterval(async () => {
        try {
            const resp = await fetch('/admin/stealth-job/status');
            const status = await resp.json();
            if (!status.running) {
                clearInterval(refreshInterval);
                location.reload();
            }
        } catch (e) {
            console.error('Status poll failed:', e);
        }
    }, 5000);
}


// ============================================================
// Init
// ============================================================

document.addEventListener('DOMContentLoaded', function() {
    // Init column selector if on companies page
    initColumnSelector();

    // Init keyboard navigation if on companies page
    initKeyboardNav();
});
