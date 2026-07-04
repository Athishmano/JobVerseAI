/**
┌─ FILE: frontend/app.js
├─ PURPOSE: Vanilla JS logic to fetch results, handle state, sort/filter, and
│           render the JobBot dashboard UI dynamically.
├─ DESIGN DECISIONS: Minimal dependencies (only Lucide icons). Uses standard DOM
│                    manipulation. Parses query params (`?run=...`) to auto-load.
└─ PATTERNS: Fetch API, Event Delegation, Template literals for HTML rendering.
*/

const state = {
    runs: [],
    currentRunTs: null,
    currentRunData: null,
    view: 'passed', // 'passed' or 'failed'
    sort: 'score_desc',
    filterText: ''
};

// DOM Elements
const els = {
    runsList: document.getElementById('runs-list'),
    runTitle: document.getElementById('current-run-title'),
    runStats: document.getElementById('current-run-stats'),
    jobsContainer: document.getElementById('jobs-container'),
    btnPassed: document.getElementById('btn-passed'),
    btnFailed: document.getElementById('btn-failed'),
    searchInput: document.getElementById('search-input'),
    sortSelect: document.getElementById('sort-select'),
    modal: document.getElementById('job-modal'),
    modalContent: document.getElementById('modal-content'),
    closeModal: document.getElementById('close-modal'),
};

// ── Initialization ────────────────────────────────────────────────────────────

async function init() {
    lucide.createIcons();
    setupEventListeners();
    
    // Parse URL for specific run
    const params = new URLSearchParams(window.location.search);
    const targetRun = params.get('run');
    
    await fetchRuns(targetRun);
}

function setupEventListeners() {
    els.btnPassed.addEventListener('click', () => setView('passed'));
    els.btnFailed.addEventListener('click', () => setView('failed'));
    
    els.searchInput.addEventListener('input', (e) => {
        state.filterText = e.target.value.toLowerCase();
        renderJobs();
    });
    
    els.sortSelect.addEventListener('change', (e) => {
        state.sort = e.target.value;
        renderJobs();
    });
    
    els.closeModal.addEventListener('click', () => {
        els.modal.classList.add('hidden');
    });
    
    // Close modal on click outside
    els.modal.addEventListener('click', (e) => {
        if (e.target === els.modal) {
            els.modal.classList.add('hidden');
        }
    });
}

// ── Data Fetching ────────────────────────────────────────────────────────────

async function fetchRuns(targetRun = null) {
    try {
        const res = await fetch('/api/v1/results');
        if (!res.ok) throw new Error('Failed to fetch runs');
        
        state.runs = await res.json();
        renderRunsList();
        
        if (state.runs.length > 0) {
            // Auto-select target run or the newest one
            const runToSelect = state.runs.includes(targetRun) ? targetRun : state.runs[0];
            await selectRun(runToSelect);
        } else {
            els.jobsContainer.innerHTML = `
                <div class="empty-state">
                    <i data-lucide="inbox" class="empty-icon"></i>
                    <p>No job runs found. Run the CLI to generate results.</p>
                </div>
            `;
            lucide.createIcons();
        }
    } catch (err) {
        console.error('Error fetching runs:', err);
    }
}

async function selectRun(timestamp) {
    state.currentRunTs = timestamp;
    
    // Update URL without reload
    const newUrl = `${window.location.pathname}?run=${timestamp}`;
    window.history.pushState({ path: newUrl }, '', newUrl);
    
    // Update Sidebar UI
    document.querySelectorAll('.run-item').forEach(el => {
        el.classList.toggle('active', el.dataset.ts === timestamp);
    });
    
    els.runTitle.textContent = "Loading...";
    els.jobsContainer.innerHTML = '<div class="empty-state"><p>Fetching results...</p></div>';
    
    try {
        const res = await fetch(`/api/v1/results/${timestamp}`);
        if (!res.ok) throw new Error('Failed to fetch run details');
        
        state.currentRunData = await res.json();
        
        const passedData = state.currentRunData.passed;
        
        els.runTitle.textContent = `Run: ${passedData.timestamp.split('/')[0]}`;
        els.runStats.innerHTML = `
            Scraped: <strong>${passedData.total_scraped}</strong> • 
            Passed: <strong style="color:var(--success)">${passedData.total_passed}</strong> • 
            Failed: <strong style="color:var(--danger)">${passedData.total_failed}</strong>
        `;
        
        renderJobs();
    } catch (err) {
        console.error('Error fetching run details:', err);
        els.jobsContainer.innerHTML = `
            <div class="empty-state">
                <i data-lucide="alert-circle" class="empty-icon"></i>
                <p>Error loading run details.</p>
            </div>
        `;
        lucide.createIcons();
    }
}

// ── Rejection reason config ───────────────────────────────────────────────────
// Maps rejection_reason → { label, icon, pillClass, detailClass }
const REJECTION_CONFIG = {
    quota_skipped:    { icon: '⏳', label: 'Rate Limited',       pill: 'pill-amber',  detail: 'detail-amber'  },
    offline_rank_cut: { icon: '📊', label: 'Rank Cut',           pill: 'pill-slate',  detail: 'detail-slate'  },
    low_ai_score:     { icon: '🤖', label: 'Low Score',          pill: 'pill-orange', detail: 'detail-orange' },
    api_error:        { icon: '⚠️', label: 'API Error',          pill: 'pill-red',    detail: 'detail-red'    },
    blacklist_keyword:{ icon: '🚫', label: 'Keyword Blocked',    pill: 'pill-red',    detail: 'detail-red'    },
    blacklist_company:{ icon: '🏢', label: 'Company Blocked',    pill: 'pill-red',    detail: 'detail-red'    },
    max_experience_exceeded: { icon: '📅', label: 'Over-experienced', pill: 'pill-red', detail: 'detail-red' },
};

function getRejectionConfig(reason) {
    return REJECTION_CONFIG[reason] || { icon: '✗', label: reason || 'Rejected', pill: 'pill-red', detail: 'detail-red' };
}

// ── Rendering ────────────────────────────────────────────────────────────

function renderRunsList() {
    els.runsList.innerHTML = state.runs.map(ts => {
        const readable = ts.replace('_', ' ');
        return `
            <li class="run-item" data-ts="${ts}" onclick="selectRun('${ts}')">
                <i data-lucide="calendar"></i>
                <span>${readable}</span>
            </li>
        `;
    }).join('');
    lucide.createIcons();
}

function setView(view) {
    state.view = view;
    els.btnPassed.classList.toggle('active', view === 'passed');
    els.btnFailed.classList.toggle('active', view === 'failed');
    
    // Hide sorting for failed jobs since score is usually null
    els.sortSelect.style.display = view === 'failed' ? 'none' : 'block';
    
    renderJobs();
}

function renderJobs() {
    if (!state.currentRunData) return;
    
    const isPassed = state.view === 'passed';
    let jobs = isPassed ? 
        state.currentRunData.passed.best_matches : 
        state.currentRunData.failed.rejected_jobs;
        
    // 1. Filter
    if (state.filterText) {
        jobs = jobs.filter(j => 
            (j.title || '').toLowerCase().includes(state.filterText) ||
            (j.company || '').toLowerCase().includes(state.filterText) ||
            (j.skills_required && j.skills_required.some(s => s.toLowerCase().includes(state.filterText)))
        );
    }
    
    // 2. Sort (only for passed jobs)
    if (isPassed) {
        jobs.sort((a, b) => {
            if (state.sort === 'score_desc') return (b.ai_score || 0) - (a.ai_score || 0);
            if (state.sort === 'score_asc') return (a.ai_score || 0) - (b.ai_score || 0);
            if (state.sort === 'date_desc') {
                // Simplistic fallback since dates are often stringly typed like "2 days ago"
                // Would need proper date parsing for perfect sort
                return String(b.posted_date || '').localeCompare(String(a.posted_date || ''));
            }
            return 0;
        });
    }
    
    // 3. Render HTML
    if (jobs.length === 0) {
        els.jobsContainer.innerHTML = `
            <div class="empty-state">
                <i data-lucide="search-X" class="empty-icon"></i>
                <p>No jobs match your criteria.</p>
            </div>
        `;
        lucide.createIcons();
        return;
    }
    
    const html = jobs.map((job, idx) => {
        const score = job.ai_score;
        let scoreBadge = '';
        if (score !== null && score !== undefined) {
            let cls = 'low';
            if (score >= 70) cls = ''; // green
            else if (score >= 40) cls = 'medium';
            scoreBadge = `<span class="score-badge ${cls}">${score}/100</span>`;
        }

        let seenBadge = '';
        if (job.is_duplicate) {
            seenBadge = `<span class="seen-badge">Seen</span>`;
        }

        let applicantHtml = '';
        if (job.applicant_count) {
            applicantHtml = `<span class="meta-item"><i data-lucide="users"></i> ${job.applicant_count}</span>`;
        }

        // ── Failed card extras ────────────────────────────────────────────────
        let rejectionBadge = '';
        let rejectionDetail = '';
        let failedClass = '';

        if (!isPassed) {
            const cfg = getRejectionConfig(job.rejection_reason);
            const scoreLabel = (job.rejection_reason === 'low_ai_score' && score !== null) ? ` (${score}/100)` : '';

            rejectionBadge = `
                <span class="rejection-pill ${cfg.pill}">
                    ${cfg.icon} ${cfg.label}${scoreLabel}
                </span>
            `;

            rejectionDetail = `
                <div class="rejection-detail ${cfg.detail}">
                    <span class="rejection-detail-label">${cfg.icon} ${cfg.label}</span>
                    <span class="rejection-detail-text">${escapeHtml(job.rejection_detail || '')}</span>
                </div>
            `;

            failedClass = ' failed';
        }

        return `
            <div class="job-card${failedClass}" onclick="openModal(${idx})">
                <div class="card-header">
                    <div style="min-width:0;flex:1;">
                        <h3 class="card-title">${escapeHtml(job.title)}</h3>
                        <div class="card-company">${escapeHtml(job.company)}</div>
                    </div>
                    <div class="card-badges">
                        ${seenBadge}
                        ${isPassed ? scoreBadge : rejectionBadge}
                    </div>
                </div>

                <div class="card-meta">
                    <span class="meta-item"><i data-lucide="map-pin"></i> ${escapeHtml(job.location || 'Unknown')}</span>
                    ${job.salary ? `<span class="meta-item"><i data-lucide="banknote"></i> ${escapeHtml(job.salary)}</span>` : ''}
                    ${job.posted_date ? `<span class="meta-item"><i data-lucide="clock"></i> ${escapeHtml(job.posted_date)}</span>` : ''}
                    ${applicantHtml}
                </div>

                ${isPassed
                    ? `<p class="card-desc">${escapeHtml(job.description || '')}</p>`
                    : rejectionDetail
                }

                <div class="tags">
                    <span class="tag">${job.source}</span>
                    ${(job.skills_required || []).slice(0, 3).map(s => `<span class="tag">${escapeHtml(s)}</span>`).join('')}
                </div>
            </div>
        `;
    }).join('');

    els.jobsContainer.innerHTML = html;
    lucide.createIcons();
}

// ── Modal ────────────────────────────────────────────────────────────

function openModal(index) {
    const isPassed = state.view === 'passed';
    const jobs = isPassed ? 
        state.currentRunData.passed.best_matches : 
        state.currentRunData.failed.rejected_jobs;
        
    const job = jobs[index];
    if (!job) return;

    let aiSection = '';
    if (isPassed && job.ai_score) {
        aiSection = `
            <div class="ai-assessment">
                <div class="ai-header">
                    <h3 style="font-family: var(--font-heading); font-size: 1.25rem;">AI Assessment</h3>
                    <div style="display: flex; gap: 0.5rem; align-items: center;">
                        ${job.is_duplicate ? '<span class="score-badge low" style="background: rgba(148, 163, 184, 0.15); color: #94a3b8; border-color: rgba(148, 163, 184, 0.3);">Seen</span>' : ''}
                        <span class="score-badge ${job.ai_score >= 70 ? '' : (job.ai_score >= 40 ? 'medium' : 'low')}">${job.ai_score}/100</span>
                    </div>
                </div>
                <p class="ai-reasoning"><strong>Reasoning:</strong> ${escapeHtml(job.reasoning)}</p>
                <div class="ai-lists">
                    <div>
                        <h4>Strengths</h4>
                        <ul>
                            ${job.strengths.map(s => `<li>${escapeHtml(s)}</li>`).join('')}
                        </ul>
                    </div>
                    <div>
                        <h4>Missing</h4>
                        <ul>
                            ${job.missing_skills.map(s => `<li style="color:var(--danger)">${escapeHtml(s)}</li>`).join('')}
                        </ul>
                    </div>
                </div>
                <div style="margin-top: 1rem; padding-top: 1rem; border-top: 1px solid var(--border)">
                    <p style="font-size: 0.9rem; color: var(--warning)"><strong>Tip:</strong> ${escapeHtml(job.improvement_tips)}</p>
                </div>
            </div>
        `;
    }

    const duplicateBadge = job.is_duplicate ? '<span class="score-badge low" style="margin-left: 0.5rem; background: rgba(148, 163, 184, 0.15); color: #94a3b8; border-color: rgba(148, 163, 184, 0.3); display: inline-block;">Seen</span>' : '';

    els.modalContent.innerHTML = `
        <h2 class="modal-title">${escapeHtml(job.title)}</h2>
        <div class="modal-company" style="display: flex; align-items: center; flex-wrap: wrap;">${escapeHtml(job.company)} • ${escapeHtml(job.location)} ${duplicateBadge}</div>
        
        ${aiSection}
        
        <h3 class="section-title">Job Description</h3>
        <div class="modal-desc">${escapeHtml(job.description || 'No description available.')}</div>
        
        <div class="action-bar">
            <a href="${job.url}" target="_blank" rel="noopener noreferrer" class="btn-primary">
                View on ${job.source} <i data-lucide="external-link" style="width: 18px; height: 18px;"></i>
            </a>
        </div>
    `;
    
    lucide.createIcons();
    els.modal.classList.remove('hidden');
}

// ── Utils ────────────────────────────────────────────────────────────

function escapeHtml(unsafe) {
    if (!unsafe) return '';
    return String(unsafe)
         .replace(/&/g, "&amp;")
         .replace(/</g, "&lt;")
         .replace(/>/g, "&gt;")
         .replace(/"/g, "&quot;")
         .replace(/'/g, "&#039;");
}

// Boot
init();
