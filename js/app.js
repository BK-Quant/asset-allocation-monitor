/**
 * 자산배분모니터 - 프론트엔드
 *
 * 모든 계산(전략 배분, 백테스트)은 서버 측 파이썬 파이프라인(scripts/build_output.py)이
 * 데이터 갱신 시점에 미리 끝내둔다. 브라우저는 data/*.json 을 읽어 그리기만 한다
 * (참고 사이트와 달리 클라이언트에서 모멘텀 계산을 반복하지 않음 — 더 가볍고 빠름).
 */

const DATA_BASE = "data";
const charts = {}; // canvasId -> Chart 인스턴스 (검색 필터링 시 파괴 후 재생성)

async function loadJSON(name) {
  const res = await fetch(`${DATA_BASE}/${name}?v=${Date.now()}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`${name} 로드 실패`);
  return res.json();
}

function fmtPct(w) {
  return `${(w * 100).toFixed(1)}%`;
}

function fmtPrice(p) {
  if (p == null) return "—";
  return p >= 1000 ? p.toLocaleString(undefined, { maximumFractionDigits: 0 }) : p.toFixed(2);
}

function scoreClass(score) {
  if (score == null) return "";
  return score >= 0 ? "pos" : "neg";
}

// 13612W 가중 모멘텀 스코어는 수익률이 아니라 가중치(12+4+2+1=19)가 곱해진 합산 스코어라
// %로 표시하면 오해를 준다(레퍼런스 사이트처럼 원시 숫자 그대로 표기).
function fmtScore(v) {
  if (v == null) return "—";
  return v.toFixed(3);
}

// ── 커스텀 지표 섹션 ──────────────────────────────────────────
function renderCustomSection(current) {
  const c = current.custom;
  const grid = document.getElementById("customGrid");
  grid.innerHTML = "";

  // DAA 카나리아 (breadth 3단계: 100% 공격 / 50-50 / 100% 방어)
  const daaCard = document.createElement("div");
  daaCard.className = "custom-card";
  const daaScores = c.daaCanary.scores;
  const breadth = c.daaCanary.breadth ?? (c.daaCanary.riskOn ? 1 : 0);
  const breadthBadgeClass = breadth >= 1 ? "on" : breadth <= 0 ? "off" : "preview";
  const breadthLabel = breadth >= 1 ? "공격형 100% (Risk-ON)" : breadth <= 0 ? "방어형 100% (Risk-OFF)" : "혼합 50/50 (완충)";
  daaCard.innerHTML = `
    <h3>${c.daaCanary.label}</h3>
    <div class="canary-row"><span>VWO (신흥국)</span><span class="${scoreClass(daaScores.VWO)}">${fmtScore(daaScores.VWO)}</span></div>
    <div class="canary-row"><span>BND (미국종합채권)</span><span class="${scoreClass(daaScores.BND)}">${fmtScore(daaScores.BND)}</span></div>
    <div class="sub">
      <span class="badge ${breadthBadgeClass}">${breadthLabel}</span>
      기준일 ${c.daaCanary.asOfDate}
    </div>
    <div class="sub">${c.daaCanary.note}</div>
  `;
  grid.appendChild(daaCard);

  // DAA 카나리아 프리뷰 (최신 종가 기준, 월말 미확정)
  if (c.daaCanaryPreview) {
    const daaPrevCard = document.createElement("div");
    daaPrevCard.className = "custom-card";
    const pScores = c.daaCanaryPreview.scores;
    const pBreadth = c.daaCanaryPreview.breadth ?? 0;
    const pBadgeClass = pBreadth >= 1 ? "on" : pBreadth <= 0 ? "off" : "preview";
    const pLabel = pBreadth >= 1 ? "공격형 100% (Risk-ON)" : pBreadth <= 0 ? "방어형 100% (Risk-OFF)" : "혼합 50/50 (완충)";
    daaPrevCard.innerHTML = `
      <h3>${c.daaCanaryPreview.label} <span class="badge preview">미확정</span></h3>
      <div class="canary-row"><span>VWO (신흥국)</span><span class="${scoreClass(pScores.VWO)}">${fmtScore(pScores.VWO)}</span></div>
      <div class="canary-row"><span>BND (미국종합채권)</span><span class="${scoreClass(pScores.BND)}">${fmtScore(pScores.BND)}</span></div>
      <div class="sub">
        <span class="badge ${pBadgeClass}">${pLabel}</span>
        기준일 ${c.daaCanaryPreview.asOfDate}
      </div>
      <div class="sub">${c.daaCanaryPreview.note}</div>
    `;
    grid.appendChild(daaPrevCard);
  }

  // ADM 당월 확정
  const admCurCard = document.createElement("div");
  admCurCard.className = "custom-card";
  admCurCard.innerHTML = `
    <h3>${c.admCurrent.label}</h3>
    <div class="big-value">${c.admCurrent.ticker ?? "USD(현금)"}</div>
    <div class="sub">${c.admCurrent.remark}</div>
    <div class="sub">기준 ${c.admCurrent.asOfDate} → 적용 <b style="color:var(--text)">${c.admCurrent.applicableMonth}</b></div>
  `;
  grid.appendChild(admCurCard);

  // ADM 차월 예상
  const admPrevCard = document.createElement("div");
  admPrevCard.className = "custom-card";
  admPrevCard.innerHTML = `
    <h3>${c.admPreview.label} <span class="badge preview">미확정</span></h3>
    <div class="big-value">${c.admPreview.ticker ?? "USD(현금)"}</div>
    <div class="sub">${c.admPreview.remark}</div>
    <div class="sub">기준 ${c.admPreview.asOfDate} → 적용(예상) <b style="color:var(--text)">${c.admPreview.applicableMonth}</b></div>
    <div class="sub">${c.admPreview.note}</div>
  `;
  grid.appendChild(admPrevCard);
}

// ── 전략 카드 ────────────────────────────────────────────────
function renderMetaBar(current) {
  const bar = document.getElementById("metaBar");
  bar.innerHTML = `
    <span>기준일(전월 말 종가) <b>${current.meta.basisDate}</b></span>
    <span>적용월 <b>${current.meta.applicableMonth}</b></span>
    <span>생성 ${current.meta.generatedAt}</span>
  `;
}

function holdingsTableHTML(holdings) {
  const rows = holdings
    .map(
      (h) => `
      <tr class="ticker-row" data-ticker="${h.ticker}" data-name="${h.displayName}">
        <td>${h.displayName}<br><span style="color:var(--text-dim);font-size:11px">${h.ticker} · ${h.sector}</span></td>
        <td class="price">${fmtPrice(h.price)}</td>
        <td class="weight">${fmtPct(h.weight)}</td>
      </tr>`
    )
    .join("");
  return `
    <table class="holdings-table">
      <thead><tr><th>티커</th><th style="text-align:right">가격</th><th style="text-align:right">비중</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function backtestStats(bt) {
  if (!bt || bt.nav.length < 2) return null;
  const nav = bt.nav;
  const years = (new Date(bt.dates[bt.dates.length - 1]) - new Date(bt.dates[0])) / (365.25 * 24 * 3600 * 1000);
  const totalReturn = nav[nav.length - 1] / nav[0] - 1;
  const cagr = years > 0 ? Math.pow(nav[nav.length - 1] / nav[0], 1 / years) - 1 : null;
  let peak = nav[0], mdd = 0;
  for (const v of nav) {
    peak = Math.max(peak, v);
    mdd = Math.min(mdd, (v - peak) / peak);
  }
  return { totalReturn, cagr, mdd, years };
}

// ── 전략별 성과 비교 ─────────────────────────────────────────
const COMPARE_COLORS = [
  "#4fd1c5", "#f0b429", "#f4694f", "#4fd18a", "#7c9cf0",
  "#c77cf0", "#f07cc4", "#7cf0d8", "#f0d87c", "#9aa8c2",
  "#4f8cf0", "#f0714f", "#71c7f0", "#c7f04f", "#f04fa8",
  "#4ff0a3", "#a34ff0",
];

function annualizedStats(bt) {
  if (!bt || bt.nav.length < 3) return null;
  const nav = bt.nav;
  const years = (new Date(bt.dates[bt.dates.length - 1]) - new Date(bt.dates[0])) / (365.25 * 24 * 3600 * 1000);
  const cagr = years > 0 ? Math.pow(nav[nav.length - 1] / nav[0], 1 / years) - 1 : null;
  let peak = nav[0], mdd = 0;
  const monthlyReturns = [];
  for (let i = 0; i < nav.length; i++) {
    peak = Math.max(peak, nav[i]);
    mdd = Math.min(mdd, (nav[i] - peak) / peak);
    if (i > 0) monthlyReturns.push(nav[i] / nav[i - 1] - 1);
  }
  const n = monthlyReturns.length;
  const mean = monthlyReturns.reduce((a, b) => a + b, 0) / n;
  const variance = monthlyReturns.reduce((a, b) => a + (b - mean) ** 2, 0) / Math.max(1, n - 1);
  const monthlyVol = Math.sqrt(variance);
  const vol = monthlyVol * Math.sqrt(12);
  const sharpe = vol > 0 ? (mean * 12) / vol : null; // 무위험이자율 0 가정
  return { cagr, mdd, vol, sharpe, years };
}

function renderComparison(current, backtests) {
  const codes = Object.keys(backtests).filter((c) => backtests[c] && backtests[c].nav.length > 1);
  if (!codes.length) {
    document.getElementById("compareSection").style.display = "none";
    return;
  }

  const rows = codes.map((code) => {
    const bt = backtests[code];
    const stats = annualizedStats(bt);
    return { code, label: current.strategies[code]?.label || code, bt, stats };
  }).filter((r) => r.stats);

  // 색상은 전체 목록 기준 고정 인덱스라 선택을 바꿔도 각 전략 색이 유지된다.
  const colorOf = (code) => COMPARE_COLORS[rows.findIndex((r) => r.code === code) % COMPARE_COLORS.length];

  // ── 전략 선택 체크박스 ──
  const picker = document.getElementById("comparePicker");
  const sortedForPicker = [...rows].sort((a, b) => a.label.localeCompare(b.label));
  picker.innerHTML = sortedForPicker
    .map(
      (r) => `
      <label class="picker-item">
        <input type="checkbox" value="${r.code}" checked />
        <i style="background:${colorOf(r.code)}"></i>
        <span>${r.label}</span>
      </label>`
    )
    .join("");

  const chartCanvas = document.getElementById("compareChart");
  const emptyHint = document.getElementById("compareEmptyHint");

  function redrawChart() {
    const selected = [...picker.querySelectorAll("input:checked")].map((el) => el.value);
    if (!selected.length) {
      chartCanvas.style.display = "none";
      emptyHint.style.display = "";
      return;
    }
    chartCanvas.style.display = "";
    emptyHint.style.display = "none";

    const selectedRows = rows.filter((r) => selected.includes(r.code));
    // 선택된 전략들끼리 공통으로 겹치는 구간(가장 늦게 시작한 전략의 시작일)부터 100으로 리베이스
    const commonStart = selectedRows.map((r) => r.bt.dates[0]).sort().pop();
    const series = selectedRows.map((r) => {
      const startIdx = r.bt.dates.findIndex((d) => d >= commonStart);
      if (startIdx < 0) return null;
      const baseNav = r.bt.nav[startIdx];
      const dates = r.bt.dates.slice(startIdx);
      const nav = r.bt.nav.slice(startIdx).map((v) => (v / baseNav) * 100);
      return { code: r.code, label: r.label, dates, nav, color: colorOf(r.code) };
    }).filter(Boolean);
    drawMultiLineChart(chartCanvas, series);
  }

  picker.addEventListener("change", redrawChart);
  document.getElementById("comparePickAll").addEventListener("click", () => {
    picker.querySelectorAll("input[type=checkbox]").forEach((el) => (el.checked = true));
    redrawChart();
  });
  document.getElementById("comparePickNone").addEventListener("click", () => {
    picker.querySelectorAll("input[type=checkbox]").forEach((el) => (el.checked = false));
    redrawChart();
  });
  redrawChart();

  // ── 비교 테이블 ──
  let sortKey = "cagr", sortDir = -1;
  const tbody = document.getElementById("compareTableBody");

  function draw() {
    const sorted = [...rows].sort((a, b) => {
      const av = sortKey === "label" ? a.label : a.stats[sortKey];
      const bv = sortKey === "label" ? b.label : b.stats[sortKey];
      if (av == null) return 1;
      if (bv == null) return -1;
      if (typeof av === "string") return sortDir * av.localeCompare(bv);
      return sortDir * (av - bv);
    });
    tbody.innerHTML = sorted
      .map(
        (r) => `
        <tr>
          <td><a href="#strategy-${r.code}" class="compare-link" data-code="${r.code}">${r.label}</a></td>
          <td class="${r.stats.cagr >= 0 ? "pos" : "neg"}">${(r.stats.cagr * 100).toFixed(1)}%</td>
          <td class="neg">${(r.stats.mdd * 100).toFixed(1)}%</td>
          <td>${(r.stats.vol * 100).toFixed(1)}%</td>
          <td>${r.stats.sharpe != null ? r.stats.sharpe.toFixed(2) : "—"}</td>
          <td>${r.stats.years.toFixed(1)}년</td>
        </tr>`
      )
      .join("");
  }
  draw();

  document.querySelectorAll("#compareTable th[data-sort]").forEach((th) => {
    th.addEventListener("click", () => {
      const key = th.dataset.sort;
      sortDir = sortKey === key ? -sortDir : -1;
      sortKey = key;
      draw();
    });
  });

  // 전략명 클릭 → 하단 "전략별 이번달 배분" 카드로 스크롤 이동 + 잠깐 강조
  tbody.addEventListener("click", (e) => {
    const link = e.target.closest(".compare-link");
    if (!link) return;
    e.preventDefault();
    const card = document.getElementById(`strategy-${link.dataset.code}`);
    if (!card) return;
    // 검색 필터로 카드가 숨겨져 있을 수 있으니 필터를 초기화하고 이동
    const searchBox = document.getElementById("searchBox");
    if (searchBox && searchBox.value) {
      searchBox.value = "";
      document.querySelectorAll(".strategy-card").forEach((c) => (c.style.display = ""));
    }
    // 세부 전략 소개(로직 설명)를 펼친 상태로 이동
    const logicWrap = card.querySelector(".logic-wrap");
    const btnLogic = card.querySelector(".btn-logic");
    if (logicWrap && !logicWrap.classList.contains("open")) {
      logicWrap.classList.add("open");
      if (btnLogic) btnLogic.textContent = "로직 설명 접기";
    }
    card.scrollIntoView({ behavior: "smooth", block: "center" });
    card.classList.add("highlight");
    setTimeout(() => card.classList.remove("highlight"), 1600);
  });
}

function drawMultiLineChart(canvas, series) {
  const height = 280;
  const dpr = window.devicePixelRatio || 1;
  const parent = canvas.parentElement;
  const parentStyle = getComputedStyle(parent);
  const horizontalPadding = parseFloat(parentStyle.paddingLeft || "0") + parseFloat(parentStyle.paddingRight || "0");
  const cssWidth = Math.max(100, (parent.clientWidth || 640) - horizontalPadding);
  canvas.style.width = cssWidth + "px";
  canvas.style.height = height + "px";
  canvas.width = cssWidth * dpr;
  canvas.height = height * dpr;
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssWidth, height);

  const padding = { top: 10, right: 10, bottom: 20, left: 50 };
  const w = cssWidth - padding.left - padding.right;
  const h = height - padding.top - padding.bottom;

  const allValues = series.flatMap((s) => s.nav);
  if (!allValues.length) return;
  const min = Math.min(...allValues);
  const max = Math.max(...allValues);
  const range = max - min || 1;
  const maxLen = Math.max(...series.map((s) => s.nav.length));

  const xFor = (i, len) => padding.left + (i / Math.max(1, len - 1)) * w;
  const yFor = (v) => padding.top + h - ((v - min) / range) * h;

  ctx.strokeStyle = "rgba(36,49,77,0.6)";
  ctx.fillStyle = "#9aa8c2";
  ctx.font = "10px sans-serif";
  ctx.textBaseline = "middle";
  [min, (min + max) / 2, max].forEach((v) => {
    const y = yFor(v);
    ctx.beginPath();
    ctx.moveTo(padding.left, y);
    ctx.lineTo(padding.left + w, y);
    ctx.stroke();
    ctx.textAlign = "right";
    ctx.fillText(v.toFixed(0), padding.left - 6, y);
  });

  series.forEach((s, i) => {
    ctx.beginPath();
    s.nav.forEach((v, idx) => {
      const x = xFor(idx, maxLen), y = yFor(v);
      if (idx === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = s.color || COMPARE_COLORS[i % COMPARE_COLORS.length];
    ctx.lineWidth = 1.5;
    ctx.stroke();
  });

  const longestSeries = series.reduce((a, b) => (b.nav.length > a.nav.length ? b : a));
  ctx.fillStyle = "#9aa8c2";
  ctx.textAlign = "left";
  ctx.fillText(longestSeries.dates[0] || "", padding.left, height - 6);
  ctx.textAlign = "right";
  ctx.fillText(longestSeries.dates[longestSeries.dates.length - 1] || "", padding.left + w, height - 6);
}

// ── 리밸런싱 계산기 ──────────────────────────────────────────
// 전략마다 구성 종목이 전부 한국 상장(KRX, *.KS → 원화) 이거나 전부 미국 상장(달러)이라
// 전략 단위로 통화를 판정한다(현재 데이터셋에는 한 전략 안에 두 통화가 섞인 경우 없음).
function strategyCurrency(rows) {
  return rows.length && rows.every((h) => h.ticker.endsWith(".KS")) ? "KRW" : "USD";
}

function fmtMoney(v, currency) {
  if (v == null || !isFinite(v)) return "—";
  return currency === "KRW" ? Math.round(v).toLocaleString() + "원" : "$" + v.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function rebalancePanelHTML(code, holdings) {
  const radioName = `rbmode-${code}`;
  const rows = holdings.filter((h) => h.weight > 0.0001);
  const currency = strategyCurrency(rows);
  const unitLabel = currency === "KRW" ? "원" : "달러";
  return `
    <div class="rebalance-tabs">
      <button type="button" class="rb-tab active" data-mode="new">신규 투자</button>
      <button type="button" class="rb-tab" data-mode="adjust">보유 조정</button>
    </div>
    <div class="rb-pane rb-pane-new">
      <div class="rb-row">
        <input type="number" class="rb-total-input" placeholder="총 투자금액(${unitLabel})" min="0" />
        <button type="button" class="btn btn-accent rb-calc-new">계산</button>
      </div>
      <div class="rb-result"></div>
    </div>
    <div class="rb-pane rb-pane-adjust" hidden>
      <div class="rb-mode-toggle">
        <label><input type="radio" name="${radioName}" value="qty" checked /> 수량으로 입력</label>
        <label><input type="radio" name="${radioName}" value="amount" /> 금액으로 입력</label>
      </div>
      <table class="rebalance-input-table">
        <thead><tr><th>종목</th><th>목표비중</th><th>현재가</th><th>보유 입력</th></tr></thead>
        <tbody>
          ${rows
            .map(
              (h) => `
            <tr>
              <td>${h.displayName}</td>
              <td>${fmtPct(h.weight)}</td>
              <td>${fmtPrice(h.price)}</td>
              <td><input type="number" class="rb-holding-input" data-ticker="${h.ticker}" placeholder="0" min="0" /></td>
            </tr>`
            )
            .join("")}
        </tbody>
      </table>
      <button type="button" class="btn btn-accent rb-calc-adjust">계산</button>
      <div class="rb-result"></div>
    </div>
  `;
}

function rebalanceResultTable(headerExtra, bodyRows) {
  return `
    <table class="rebalance-result-table">
      <thead><tr><th>종목</th><th>목표비중</th>${headerExtra}<th>조정금액</th><th>조정수량</th></tr></thead>
      <tbody>${bodyRows}</tbody>
    </table>`;
}

function wireRebalancePanel(card, holdings) {
  const wrap = card.querySelector(".rebalance-wrap");
  const rows = holdings.filter((h) => h.weight > 0.0001);
  const currency = strategyCurrency(rows);

  // 탭 전환
  wrap.querySelectorAll(".rb-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      wrap.querySelectorAll(".rb-tab").forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      wrap.querySelector(".rb-pane-new").hidden = tab.dataset.mode !== "new";
      wrap.querySelector(".rb-pane-adjust").hidden = tab.dataset.mode !== "adjust";
    });
  });

  // 신규 투자: 총액 입력 → 종목별 매수금액·수량
  wrap.querySelector(".rb-calc-new").addEventListener("click", () => {
    const total = parseFloat(wrap.querySelector(".rb-total-input").value);
    const resultEl = wrap.querySelector(".rb-pane-new .rb-result");
    if (!(total > 0)) {
      resultEl.innerHTML = `<p class="rb-hint">총 투자금액을 입력해주세요.</p>`;
      return;
    }
    const body = rows
      .map((h) => {
        const targetAmount = total * h.weight;
        const qty = h.price ? Math.floor(targetAmount / h.price) : null;
        return `
        <tr>
          <td>${h.displayName}</td>
          <td>${fmtPct(h.weight)}</td>
          <td class="pos">${fmtMoney(targetAmount, currency)}</td>
          <td>${qty != null ? qty.toLocaleString() + "주" : "—"}</td>
        </tr>`;
      })
      .join("");
    resultEl.innerHTML = rebalanceResultTable("", body);
  });

  // 보유 조정: 종목별 현재 보유(수량 또는 금액) → 목표비중 기준 조정 필요분
  wrap.querySelector(".rb-calc-adjust").addEventListener("click", () => {
    const mode = wrap.querySelector('input[name^="rbmode-"]:checked').value;
    const resultEl = wrap.querySelector(".rb-pane-adjust .rb-result");
    const inputs = [...wrap.querySelectorAll(".rb-holding-input")];
    const current = {};
    let totalValue = 0;
    for (const input of inputs) {
      const h = rows.find((r) => r.ticker === input.dataset.ticker);
      const raw = parseFloat(input.value) || 0;
      const value = mode === "qty" ? raw * (h.price || 0) : raw;
      current[h.ticker] = value;
      totalValue += value;
    }
    if (totalValue <= 0) {
      resultEl.innerHTML = `<p class="rb-hint">현재 보유 ${mode === "qty" ? "수량" : "금액"}을 하나 이상 입력해주세요.</p>`;
      return;
    }
    const body = rows
      .map((h) => {
        const currentValue = current[h.ticker] || 0;
        const targetValue = totalValue * h.weight;
        const diffValue = targetValue - currentValue;
        const diffQty = h.price ? Math.round(diffValue / h.price) : null;
        const diffClass = diffValue >= 0 ? "pos" : "neg";
        const diffLabel = diffValue >= 0 ? "매수" : "매도";
        return `
        <tr>
          <td>${h.displayName}</td>
          <td>${fmtPct(h.weight)}</td>
          <td>${fmtMoney(currentValue, currency)}</td>
          <td class="${diffClass}">${diffLabel} ${fmtMoney(Math.abs(diffValue), currency)}</td>
          <td class="${diffClass}">${diffQty != null && diffQty !== 0 ? (diffQty > 0 ? "+" : "-") + Math.abs(diffQty).toLocaleString() + "주" : diffQty === 0 ? "0주" : "—"}</td>
        </tr>`;
      })
      .join("");
    resultEl.innerHTML = `
      <p class="rb-hint">현재 평가금액 합계: <b>${fmtMoney(totalValue, currency)}</b> · 조정수량은 정수 주 단위로 반올림한 참고값입니다.</p>
      <table class="rebalance-result-table">
        <thead><tr><th>종목</th><th>목표비중</th><th>현재금액</th><th>조정금액</th><th>조정수량</th></tr></thead>
        <tbody>${body}</tbody>
      </table>`;
  });
}

function renderStrategies(current, backtests, prices) {
  const list = document.getElementById("strategyList");
  list.innerHTML = "";

  const entries = Object.entries(current.strategies);
  for (const [code, s] of entries) {
    const card = document.createElement("div");
    card.className = "strategy-card";
    card.id = `strategy-${code}`;
    card.dataset.search = `${s.label} ${code} ${s.holdings.map((h) => h.ticker + " " + h.displayName).join(" ")}`.toLowerCase();

    const bt = backtests[code];
    const stats = backtestStats(bt);
    const statsHTML = stats
      ? `<div class="backtest-stats">
           <span>총수익 <b>${(stats.totalReturn * 100).toFixed(0)}%</b></span>
           <span>CAGR <b>${stats.cagr != null ? (stats.cagr * 100).toFixed(1) + "%" : "—"}</b></span>
           <span>MDD <b>${(stats.mdd * 100).toFixed(1)}%</b></span>
           <span>${stats.years.toFixed(1)}년</span>
         </div>`
      : `<div class="backtest-stats">백테스트 데이터 없음</div>`;

    card.innerHTML = `
      <h3>${s.label}</h3>
      <div class="timing-note">${s.timingNote}</div>
      ${holdingsTableHTML(s.holdings)}
      <div class="card-actions">
        <button class="btn btn-logic" type="button">로직 설명</button>
        <button class="btn btn-backtest" type="button">백테스트 보기</button>
        <button class="btn btn-rebalance" type="button">리밸런싱 계산기</button>
      </div>
      <div class="logic-wrap">${s.description || "설명이 아직 없습니다."}</div>
      <div class="backtest-wrap">
        <canvas></canvas>
        ${statsHTML}
      </div>
      <div class="rebalance-wrap">${rebalancePanelHTML(code, s.holdings)}</div>
    `;
    list.appendChild(card);
    wireRebalancePanel(card, s.holdings);

    // 티커 클릭 → 가격 차트 모달
    card.querySelectorAll(".ticker-row").forEach((row) => {
      row.addEventListener("click", () => openTickerModal(row.dataset.ticker, row.dataset.name, prices));
    });

    // 로직 설명 토글
    const btnLogic = card.querySelector(".btn-logic");
    const logicWrap = card.querySelector(".logic-wrap");
    btnLogic.addEventListener("click", () => {
      logicWrap.classList.toggle("open");
      btnLogic.textContent = logicWrap.classList.contains("open") ? "로직 설명 접기" : "로직 설명";
    });

    // 백테스트 토글
    const btnBt = card.querySelector(".btn-backtest");
    const btWrap = card.querySelector(".backtest-wrap");
    let drawn = false;
    btnBt.addEventListener("click", () => {
      const opening = !btWrap.classList.contains("open");
      btWrap.classList.toggle("open");
      if (opening && !drawn && bt) {
        drawBacktestChart(btWrap.querySelector("canvas"), bt);
        drawn = true;
      }
      btnBt.textContent = btWrap.classList.contains("open") ? "백테스트 접기" : "백테스트 보기";
    });

    // 리밸런싱 계산기 토글
    const btnRebalance = card.querySelector(".btn-rebalance");
    const rebalanceWrap = card.querySelector(".rebalance-wrap");
    btnRebalance.addEventListener("click", () => {
      rebalanceWrap.classList.toggle("open");
      btnRebalance.textContent = rebalanceWrap.classList.contains("open") ? "리밸런싱 계산기 접기" : "리밸런싱 계산기";
    });
  }
}

/**
 * 의존성 없는 간단한 캔버스 라인차트.
 * (외부 CDN을 쓰지 않는다 — 사내 프록시/방화벽 환경에서도, 그리고 이 앱을 어디에
 * 호스팅하든 항상 동작하도록 하기 위함)
 */
function drawLineChart(canvas, values, dates, opts = {}) {
  const { color = "#4fd1c5", fill = "rgba(79,209,197,0.10)", height = 160, yFormat = (v) => v.toFixed(1) } = opts;
  const dpr = window.devicePixelRatio || 1;
  const cssWidth = canvas.parentElement.clientWidth || 320;
  canvas.style.width = cssWidth + "px";
  canvas.style.height = height + "px";
  canvas.width = cssWidth * dpr;
  canvas.height = height * dpr;
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssWidth, height);

  const padding = { top: 10, right: 8, bottom: 18, left: 46 };
  const w = cssWidth - padding.left - padding.right;
  const h = height - padding.top - padding.bottom;

  const valid = values.filter((v) => v != null);
  if (!valid.length) return;
  const min = Math.min(...valid);
  const max = Math.max(...valid);
  const range = max - min || 1;

  const xFor = (i) => padding.left + (i / Math.max(1, values.length - 1)) * w;
  const yFor = (v) => padding.top + h - ((v - min) / range) * h;

  // 그리드 + y축 라벨 (최소/중간/최대)
  ctx.strokeStyle = "rgba(36,49,77,0.6)";
  ctx.fillStyle = "#9aa8c2";
  ctx.font = "10px sans-serif";
  ctx.textBaseline = "middle";
  [min, (min + max) / 2, max].forEach((v) => {
    const y = yFor(v);
    ctx.beginPath();
    ctx.moveTo(padding.left, y);
    ctx.lineTo(padding.left + w, y);
    ctx.stroke();
    ctx.textAlign = "right";
    ctx.fillText(yFormat(v), padding.left - 6, y);
  });

  // 선 + 아래 채우기
  ctx.beginPath();
  let started = false;
  values.forEach((v, i) => {
    if (v == null) return;
    const x = xFor(i), y = yFor(v);
    if (!started) {
      ctx.moveTo(x, y);
      started = true;
    } else {
      ctx.lineTo(x, y);
    }
  });
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.5;
  ctx.stroke();
  ctx.lineTo(xFor(values.length - 1), padding.top + h);
  ctx.lineTo(xFor(0), padding.top + h);
  ctx.closePath();
  ctx.fillStyle = fill;
  ctx.fill();

  // x축 라벨 (시작/끝 날짜)
  ctx.fillStyle = "#9aa8c2";
  ctx.textAlign = "left";
  ctx.fillText(dates[0] || "", padding.left, height - 6);
  ctx.textAlign = "right";
  ctx.fillText(dates[dates.length - 1] || "", padding.left + w, height - 6);
}

function drawBacktestChart(canvas, bt) {
  drawLineChart(canvas, bt.nav, bt.dates, { color: "#4fd1c5", fill: "rgba(79,209,197,0.10)", height: 160, yFormat: (v) => v.toFixed(0) });
}

// ── 티커 가격 모달 ───────────────────────────────────────────
function openTickerModal(ticker, name, prices) {
  const root = document.getElementById("modalRoot");
  const body = document.getElementById("modalBody");
  const arr = prices[ticker];
  const dates = prices.dates;

  if (!arr) {
    body.innerHTML = `<h3>${ticker}</h3><p>가격 데이터가 없습니다.</p>`;
  } else {
    // 최근 3년치만 표시 (전체 15년치는 너무 촘촘함)
    const showDays = 756;
    const start = Math.max(0, arr.length - showDays);
    const sliceDates = dates.slice(start);
    const sliceValues = arr.slice(start);
    body.innerHTML = `<h3>${ticker} <span style="color:var(--text-dim);font-weight:400">${name || ""}</span></h3>
      <div><canvas id="tickerChartCanvas"></canvas></div>
      <p style="color:var(--text-dim);font-size:12px;margin-top:10px">최근 약 3년(756거래일) 조정종가 · 전체 히스토리는 ${dates[0]}부터</p>`;
    const canvas = document.getElementById("tickerChartCanvas");
    drawLineChart(canvas, sliceValues, sliceDates, { color: "#f0b429", fill: "rgba(240,180,41,0.10)", height: 240 });
  }

  root.hidden = false;
}

document.addEventListener("click", (e) => {
  if (e.target.matches("[data-close]")) {
    document.getElementById("modalRoot").hidden = true;
  }
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") document.getElementById("modalRoot").hidden = true;
});

// ── 검색 필터 ────────────────────────────────────────────────
function wireSearch() {
  const box = document.getElementById("searchBox");
  box.addEventListener("input", () => {
    const q = box.value.trim().toLowerCase();
    document.querySelectorAll(".strategy-card").forEach((card) => {
      card.style.display = !q || card.dataset.search.includes(q) ? "" : "none";
    });
  });
}

// ── 부트스트랩 ───────────────────────────────────────────────
async function main() {
  try {
    const [current, backtests, prices] = await Promise.all([
      loadJSON("current.json"),
      loadJSON("backtests.json"),
      loadJSON("prices.json"),
    ]);
    renderMetaBar(current);
    renderCustomSection(current);
    renderComparison(current, backtests);
    renderStrategies(current, backtests, prices);
    wireSearch();
  } catch (err) {
    console.error(err);
    document.getElementById("strategyList").innerHTML = `<p style="color:#f4694f">데이터를 불러오지 못했습니다: ${err.message}</p>`;
  }
}

main();
