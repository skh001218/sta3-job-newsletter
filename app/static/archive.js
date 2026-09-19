"use strict";

const archiveState = {
  attempts: [],
  selectedId: null,
};

const el = (id) => document.getElementById(id);

function authHeaders() {
  const token = sessionStorage.getItem("app-access-token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function api(path, mayPrompt = true) {
  const response = await fetch(path, { headers: authHeaders() });
  if (response.status === 401 && mayPrompt) {
    const token = window.prompt("이 서버의 APP_ACCESS_TOKEN을 입력해 주세요.");
    if (token) {
      sessionStorage.setItem("app-access-token", token.trim());
      return api(path, false);
    }
  }
  const payload = await response.json().catch(() => ({ error: "서버 응답을 읽지 못했습니다." }));
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload;
}

function formatDate(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "저장 날짜 미상";
  return new Intl.DateTimeFormat("ko-KR", {
    year: "numeric",
    month: "long",
    day: "numeric",
  }).format(date);
}

function detail(label, value, wide = false) {
  const wrapper = document.createElement("dl");
  wrapper.className = `detail${wide ? " wide" : ""}`;
  const term = document.createElement("dt");
  term.textContent = label;
  const description = document.createElement("dd");
  description.textContent = value || "-";
  wrapper.append(term, description);
  return wrapper;
}

function resultSection(title, value) {
  const section = document.createElement("section");
  section.className = "result-section";
  const heading = document.createElement("h3");
  heading.textContent = title;
  section.append(heading);
  if (Array.isArray(value)) {
    const list = document.createElement("ul");
    for (const item of value.length ? value : ["기록된 내용이 없습니다."]) {
      const row = document.createElement("li");
      row.textContent = item;
      list.append(row);
    }
    section.append(list);
  } else {
    const paragraph = document.createElement("p");
    paragraph.textContent = value || "-";
    section.append(paragraph);
  }
  return section;
}

function renderList() {
  el("archive-page-count").textContent = String(archiveState.attempts.length);
  const list = el("archive-page-list");
  list.replaceChildren();
  for (const attempt of archiveState.attempts) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "archive-index-item";
    button.classList.toggle("active", attempt.attempt_id === archiveState.selectedId);
    button.setAttribute("aria-pressed", String(attempt.attempt_id === archiveState.selectedId));
    const title = document.createElement("strong");
    title.textContent = attempt.title;
    const meta = document.createElement("span");
    meta.textContent = `${attempt.publisher} · ${formatDate(attempt.archived_at)}`;
    button.append(title, meta);
    button.addEventListener("click", () => selectAttempt(attempt.attempt_id, true));
    list.append(button);
  }
}

function renderDetail(attempt) {
  const question = attempt.question;
  const response = attempt.response;
  const comparison = attempt.comparison;
  const options = Object.fromEntries(question.options.map((option) => [option.id, option.label]));

  el("archive-detail-source").textContent = `${attempt.publisher} · 문제 v${question.version}`;
  el("archive-detail-title").textContent = question.title;
  el("archive-detail-date").textContent = `${formatDate(attempt.archived_at)} 보관`;
  el("archive-detail-scenario").textContent = question.scenario;
  el("archive-detail-prompt").textContent = question.prompt || "어떤 판단을 내리시겠어요?";

  el("archive-my-answer").replaceChildren(
    detail("내 선택", `${response.selected_option}. ${options[response.selected_option] || ""}`, true),
    detail("선택 이유", response.reason, true),
    detail("예상 결과", response.expected_outcome),
    detail("세운 가정", response.assumptions),
    detail("확신도", `${response.confidence}%`)
  );

  const recommended = comparison.recommended_option;
  const sameChoice = recommended && recommended === response.selected_option;
  const summary = document.createElement("div");
  summary.className = `difference-summary ${sameChoice ? "same" : "different"}`;
  const summaryLabel = document.createElement("span");
  summaryLabel.textContent = sameChoice ? "추천 판단과 같은 선택" : "내 선택과 추천 판단이 달랐어요";
  const summaryText = document.createElement("strong");
  summaryText.textContent = recommended
    ? `내 선택 ${response.selected_option} → 추천 ${recommended}. ${options[recommended] || ""}`
    : "추천 선택지는 자료에서 확인되지 않았습니다.";
  summary.append(summaryLabel, summaryText);

  const difference = el("archive-difference");
  difference.replaceChildren(
    summary,
    resultSection("왜 이런 판단이 추천됐나요?", comparison.recommendation_reason),
    resultSection("실제 사례에서 선택한 방법", comparison.actual_action),
    resultSection("실제 결과", comparison.actual_outcome),
    resultSection("내 답에서 놓친 점", comparison.missing_considerations),
    resultSection("그래도 잘 고려한 점", comparison.well_considered),
    resultSection("다음에 확인할 질문", comparison.next_questions)
  );
}

function selectAttempt(attemptId, updateUrl = false) {
  const attempt = archiveState.attempts.find((item) => item.attempt_id === attemptId);
  if (!attempt) return;
  archiveState.selectedId = attemptId;
  renderList();
  renderDetail(attempt);
  if (updateUrl) {
    const url = new URL(window.location.href);
    url.searchParams.set("attempt", attemptId);
    history.pushState({ attemptId }, "", url);
  }
  if (window.matchMedia("(max-width: 820px)").matches) {
    el("archive-detail").scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

async function initializeArchive() {
  try {
    const payload = await api("/api/archive");
    archiveState.attempts = payload.attempts || [];
    el("archive-loading").hidden = true;
    if (!archiveState.attempts.length) {
      el("archive-empty-page").hidden = false;
      return;
    }
    el("archive-layout").hidden = false;
    const requested = new URLSearchParams(window.location.search).get("attempt");
    const initial = archiveState.attempts.some((item) => item.attempt_id === requested)
      ? requested
      : archiveState.attempts[0].attempt_id;
    selectAttempt(initial);
  } catch (error) {
    el("archive-loading").hidden = true;
    const notice = el("archive-notice");
    notice.textContent = error.message;
    notice.hidden = false;
  }
}

window.addEventListener("popstate", () => {
  const attemptId = new URLSearchParams(window.location.search).get("attempt");
  if (attemptId) selectAttempt(attemptId);
});

initializeArchive();
