"use strict";

const el = (id) => document.getElementById(id);
let activity = [];

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

function renderStreak(data) {
  el("streak-count").textContent = data.current_streak;
  const status = el("streak-status");
  status.classList.toggle("done", data.solved_today);
  status.textContent = data.solved_today ? "오늘 학습 완료" : "오늘 학습 전";
  el("streak-message").textContent = data.solved_today
    ? "좋아요. 내일도 한 문제로 흐름을 이어가세요."
    : data.current_streak > 0
      ? "오늘 한 문제를 풀면 연속 기록이 이어져요."
      : "오늘 첫 문제로 새로운 기록을 시작해 보세요.";
}

function svgElement(name, attributes = {}) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", name);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
  return node;
}

function shortDate(date) {
  return new Intl.DateTimeFormat("ko-KR", { month: "numeric", day: "numeric" })
    .format(new Date(`${date}T00:00:00`));
}

function renderChart() {
  const container = el("activity-chart");
  const width = Math.max(320, Math.floor(container.clientWidth));
  const height = width < 560 ? 300 : 330;
  const margin = { top: 30, right: 14, bottom: 52, left: 48 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const yMax = Math.max(2, ...activity.map((item) => item.count));
  const tickSteps = Math.min(4, yMax);
  const slot = plotWidth / activity.length;
  const barWidth = Math.max(8, Math.min(34, slot * .58));

  const svg = svgElement("svg", {
    viewBox: `0 0 ${width} ${height}`,
    role: "img",
    "aria-label": `최근 14일 날짜별 문제 풀이 막대그래프. 총 ${activity.reduce((sum, item) => sum + item.count, 0)}문제`,
  });
  const title = svgElement("title");
  title.textContent = "최근 14일 날짜별 푼 문제 수";
  svg.append(title);

  for (let index = 0; index <= tickSteps; index += 1) {
    const value = Math.round((tickSteps - index) * yMax / tickSteps);
    const y = margin.top + (index / tickSteps) * plotHeight;
    svg.append(svgElement("line", { x1: margin.left, y1: y, x2: width - margin.right, y2: y, class: "chart-gridline" }));
    const label = svgElement("text", { x: margin.left - 10, y: y + 4, class: "chart-axis-label", "text-anchor": "end" });
    label.textContent = value;
    svg.append(label);
  }

  activity.forEach((item, index) => {
    const barHeight = item.count === 0 ? 0 : Math.max(4, item.count / yMax * plotHeight);
    const x = margin.left + index * slot + (slot - barWidth) / 2;
    const y = margin.top + plotHeight - barHeight;
    const bar = svgElement("rect", {
      x, y, width: barWidth, height: barHeight,
      rx: 3,
      class: item.date === activity[activity.length - 1].date ? "activity-bar today" : "activity-bar",
      "aria-label": `${shortDate(item.date)} ${item.count}문제`,
    });
    const barTitle = svgElement("title");
    barTitle.textContent = `${shortDate(item.date)} · ${item.count}문제`;
    bar.append(barTitle);
    svg.append(bar);

    if (item.count > 0) {
      const value = svgElement("text", { x: x + barWidth / 2, y: y - 8, class: "chart-value", "text-anchor": "middle" });
      value.textContent = item.count;
      svg.append(value);
    }

    if (index % 2 === 0 || index === activity.length - 1) {
      const date = svgElement("text", {
        x: x + barWidth / 2,
        y: height - 24,
        class: "chart-axis-label",
        "text-anchor": "middle",
      });
      date.textContent = shortDate(item.date);
      svg.append(date);
    }
  });

  const yTitle = svgElement("text", { x: 13, y: margin.top + plotHeight / 2, class: "chart-axis-title", transform: `rotate(-90 13 ${margin.top + plotHeight / 2})`, "text-anchor": "middle" });
  yTitle.textContent = "푼 문제 수";
  svg.append(yTitle);
  const xTitle = svgElement("text", { x: margin.left + plotWidth / 2, y: height - 4, class: "chart-axis-title", "text-anchor": "middle" });
  xTitle.textContent = "날짜";
  svg.append(xTitle);
  container.replaceChildren(svg);
}

async function initialize() {
  try {
    const data = await api("/api/dashboard");
    activity = data.daily_counts;
    renderStreak(data);
    const total = activity.reduce((sum, item) => sum + item.count, 0);
    el("activity-total").textContent = `최근 14일 · ${total}문제`;
    renderChart();
    new ResizeObserver(renderChart).observe(el("activity-chart"));
  } catch (error) {
    const notice = el("dashboard-error");
    notice.textContent = error.message;
    notice.hidden = false;
  }
}

initialize();
