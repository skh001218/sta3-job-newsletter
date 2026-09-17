"use strict";

const state = {
  question: null,
  attempt: null,
  pollTimer: null,
  submitConfirmed: false,
};

const el = (id) => document.getElementById(id);

function uuid() {
  if (crypto.randomUUID) return crypto.randomUUID();
  return `${Date.now()}-${crypto.getRandomValues(new Uint32Array(2)).join("-")}`;
}

function authHeaders() {
  const token = sessionStorage.getItem("app-access-token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function api(path, options = {}, mayPrompt = true) {
  const response = await fetch(path, {
    ...options,
    headers: {
      ...authHeaders(),
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...(options.headers || {}),
    },
  });
  if (response.status === 401 && mayPrompt) {
    const token = window.prompt("이 서버의 APP_ACCESS_TOKEN을 입력해 주세요.");
    if (token) {
      sessionStorage.setItem("app-access-token", token.trim());
      return api(path, options, false);
    }
  }
  const payload = await response.json().catch(() => ({ error: "서버 응답을 읽지 못했습니다." }));
  if (!response.ok && response.status !== 202) {
    const error = new Error(payload.error || `HTTP ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return payload;
}

function notice(message, success = false) {
  const box = el("notice");
  box.textContent = message;
  box.classList.toggle("success", success);
  box.hidden = false;
}

function clearNotice() {
  el("notice").hidden = true;
}

function draftKey(question) {
  return `draft:${question.id}:${question.version}`;
}

function saveDraft() {
  if (!state.question || state.attempt) return;
  const data = Object.fromEntries(new FormData(el("answer-form")).entries());
  localStorage.setItem(draftKey(state.question), JSON.stringify(data));
}

function restoreDraft(question) {
  const raw = localStorage.getItem(draftKey(question));
  if (!raw) return;
  try {
    const draft = JSON.parse(raw);
    const form = el("answer-form");
    for (const [name, value] of Object.entries(draft)) {
      const control = form.elements.namedItem(name);
      if (!control) continue;
      if (control instanceof RadioNodeList) {
        for (const item of control) item.checked = item.value === value;
      } else {
        control.value = value;
      }
    }
    el("confidence-output").textContent = `${form.elements.confidence.value}%`;
  } catch {
    localStorage.removeItem(draftKey(question));
  }
}

function renderQuestion(question) {
  state.question = question;
  el("loading").hidden = true;
  el("result-view").hidden = true;
  el("answer-view").hidden = false;
  el("question-source").textContent = `${question.source?.publisher || "출처 미상"} · 문제 v${question.version}`;
  el("question-title").textContent = question.title;
  el("question-scenario").textContent = question.scenario;
  el("question-prompt").textContent = question.prompt;
  const options = el("options");
  options.replaceChildren();
  for (const option of question.options) {
    const label = document.createElement("label");
    label.className = "option";
    const radio = document.createElement("input");
    radio.type = "radio";
    radio.name = "selected_option";
    radio.value = option.id;
    radio.required = true;
    const text = document.createElement("span");
    text.textContent = `${option.id}. ${option.label}`;
    label.append(radio, text);
    options.append(label);
  }
  restoreDraft(question);
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

function resultSection(title, value, metrics = false) {
  const section = document.createElement("section");
  section.className = "result-section";
  const heading = document.createElement("h3");
  heading.textContent = title;
  section.append(heading);
  if (Array.isArray(value)) {
    const list = document.createElement("ul");
    if (metrics) list.className = "metric-list";
    for (const item of value.length ? value : ["확인된 내용이 없습니다."]) {
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

function renderAttempt(attempt) {
  state.attempt = attempt;
  state.question = attempt.question;
  localStorage.setItem("current-attempt-id", attempt.attempt_id);
  el("loading").hidden = true;
  el("answer-view").hidden = true;
  el("result-view").hidden = false;
  const options = Object.fromEntries(attempt.question.options.map((item) => [item.id, item.label]));
  el("frozen-title").textContent = attempt.question.title;
  const frozen = el("frozen-answer");
  frozen.replaceChildren(
    detail("선택", `${attempt.response.selected_option}. ${options[attempt.response.selected_option] || ""}`, true),
    detail("선택 이유", attempt.response.reason, true),
    detail("예상 결과", attempt.response.expected_outcome),
    detail("가정", attempt.response.assumptions),
    detail("확신도", `${attempt.response.confidence}%`)
  );

  const comparison = el("comparison");
  comparison.replaceChildren();
  const completeButton = el("complete-button");
  if (attempt.comparison) {
    comparison.append(
      resultSection("실제 사례에서 선택한 방법", attempt.comparison.actual_action),
      resultSection("실제 결과", attempt.comparison.actual_outcome),
      resultSection("결과 지표", attempt.comparison.metrics, true),
      resultSection("잘 고려한 점", attempt.comparison.well_considered),
      resultSection("더 고려했으면 좋았을 점", attempt.comparison.missing_considerations),
      resultSection("결과와 별개로 합리적이었던 판단", attempt.comparison.reasonable_despite_outcome),
      resultSection("자료만으로 확인할 수 없는 점", attempt.comparison.unknown_from_evidence),
      resultSection("다음 문제에서 확인할 질문", attempt.comparison.next_questions)
    );
    completeButton.hidden = false;
  } else {
    comparison.append(resultSection("비교 결과 생성 실패", attempt.last_error || "답변은 안전하게 저장되었습니다."));
    const retryComparison = document.createElement("button");
    retryComparison.type = "button";
    retryComparison.className = "secondary";
    retryComparison.textContent = "비교 다시 생성";
    retryComparison.addEventListener("click", retryComparisonResult);
    comparison.append(retryComparison);
    completeButton.hidden = true;
  }

  const evidence = el("evidence");
  evidence.replaceChildren();
  if (attempt.comparison?.evidence_links?.length) {
    const section = resultSection("근거 자료", []);
    const list = section.querySelector("ul");
    list.replaceChildren();
    for (const link of attempt.comparison.evidence_links) {
      const item = document.createElement("li");
      const anchor = document.createElement("a");
      anchor.className = "evidence-link";
      anchor.href = link.url;
      anchor.target = "_blank";
      anchor.rel = "noreferrer";
      anchor.textContent = link.label;
      item.append(anchor);
      list.append(item);
    }
    evidence.append(section);
  }
  updateNotionControls(attempt);
}

function updateNotionControls(attempt) {
  const complete = el("complete-button");
  const retry = el("retry-button");
  const link = el("notion-link");
  complete.disabled = false;
  retry.hidden = true;
  link.hidden = true;
  if (!attempt.comparison) return;
  if (["PENDING", "SYNCING", "RETRY"].includes(attempt.notion_status)) {
    complete.disabled = true;
    complete.textContent = "Notion에 저장하는 중…";
    schedulePoll();
  } else if (attempt.notion_status === "SAVED") {
    complete.hidden = true;
    if (attempt.notion_url) {
      link.href = attempt.notion_url;
      link.hidden = false;
    }
    notice("SQLite와 Notion에 풀이 기록을 저장했습니다.", true);
  } else if (attempt.notion_status === "FAILED") {
    complete.hidden = true;
    retry.hidden = false;
    notice(attempt.last_error || "Notion 저장에 실패했습니다. 답변은 SQLite에 보존되어 있습니다.");
  } else {
    complete.hidden = false;
    complete.textContent = "완료하고 Notion에 저장";
  }
}

function schedulePoll() {
  clearTimeout(state.pollTimer);
  state.pollTimer = setTimeout(async () => {
    if (!state.attempt) return;
    try {
      const attempt = await api(`/api/attempts/${state.attempt.attempt_id}`);
      renderAttempt(attempt);
    } catch (error) {
      notice(error.message);
    }
  }, 2500);
}

async function submitAnswer(event) {
  event.preventDefault();
  clearNotice();
  if (!state.submitConfirmed) {
    el("confirm-dialog").showModal();
    return;
  }
  state.submitConfirmed = false;
  const form = event.currentTarget;
  const button = el("submit-button");
  button.disabled = true;
  const values = Object.fromEntries(new FormData(form).entries());
  const keyName = `submit-key:${state.question.id}:${state.question.version}`;
  let key = localStorage.getItem(keyName);
  if (!key) {
    key = uuid();
    localStorage.setItem(keyName, key);
  }
  try {
    const attempt = await api("/api/attempts", {
      method: "POST",
      body: JSON.stringify({
        idempotency_key: key,
        question_id: state.question.id,
        question_version: state.question.version,
        response: {
          selected_option: values.selected_option,
          reason: values.reason,
          expected_outcome: values.expected_outcome,
          assumptions: values.assumptions,
          confidence: Number(values.confidence),
        },
      }),
    });
    localStorage.removeItem(draftKey(state.question));
    renderAttempt(attempt);
    window.scrollTo({ top: 0, behavior: "smooth" });
  } catch (error) {
    notice(error.message);
  } finally {
    button.disabled = false;
  }
}

async function retryComparisonResult() {
  try {
    const attempt = await api(`/api/attempts/${state.attempt.attempt_id}/comparison/retry`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    renderAttempt(attempt);
  } catch (error) {
    notice(error.message);
  }
}

async function completeAttempt() {
  const button = el("complete-button");
  button.disabled = true;
  clearNotice();
  const keyName = `notion-key:${state.attempt.attempt_id}`;
  let key = localStorage.getItem(keyName);
  if (!key) {
    key = uuid();
    localStorage.setItem(keyName, key);
  }
  try {
    const attempt = await api(`/api/attempts/${state.attempt.attempt_id}/complete`, {
      method: "POST",
      body: JSON.stringify({ idempotency_key: key }),
    });
    renderAttempt(attempt);
  } catch (error) {
    notice(error.message);
    button.disabled = false;
  }
}

async function retryNotion() {
  el("retry-button").disabled = true;
  clearNotice();
  try {
    const attempt = await api(`/api/attempts/${state.attempt.attempt_id}/notion/retry`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    renderAttempt(attempt);
  } catch (error) {
    notice(error.message);
  } finally {
    el("retry-button").disabled = false;
  }
}

function newAttempt() {
  if (!state.question) return;
  clearTimeout(state.pollTimer);
  localStorage.removeItem("current-attempt-id");
  localStorage.removeItem(`submit-key:${state.question.id}:${state.question.version}`);
  localStorage.removeItem(draftKey(state.question));
  state.attempt = null;
  el("answer-form").reset();
  el("confidence-output").textContent = "50%";
  clearNotice();
  renderQuestion(state.question);
  window.scrollTo({ top: 0, behavior: "smooth" });
}

async function initialize() {
  el("answer-form").addEventListener("submit", submitAnswer);
  el("answer-form").addEventListener("input", saveDraft);
  el("answer-form").elements.confidence.addEventListener("input", (event) => {
    el("confidence-output").textContent = `${event.target.value}%`;
  });
  el("complete-button").addEventListener("click", completeAttempt);
  el("retry-button").addEventListener("click", retryNotion);
  el("new-attempt-button").addEventListener("click", newAttempt);
  el("confirm-cancel").addEventListener("click", () => el("confirm-dialog").close());
  el("confirm-submit").addEventListener("click", () => {
    el("confirm-dialog").close();
    state.submitConfirmed = true;
    el("answer-form").requestSubmit();
  });

  const attemptId = localStorage.getItem("current-attempt-id");
  if (attemptId) {
    try {
      renderAttempt(await api(`/api/attempts/${attemptId}`));
      return;
    } catch (error) {
      if (error.status !== 404) {
        notice(error.message);
        el("loading").hidden = true;
        return;
      }
      localStorage.removeItem("current-attempt-id");
    }
  }
  try {
    renderQuestion(await api("/api/questions/current"));
  } catch (error) {
    el("loading").textContent = error.message;
    notice(error.message);
  }
}

initialize();
