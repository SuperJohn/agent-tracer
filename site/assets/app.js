const state = {
  traces: null,
  metrics: null,
  selectedSpan: null,
};

const $ = (selector, root = document) => root.querySelector(selector);

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    }
    else if (key === "pressed") node.setAttribute("aria-pressed", value ? "true" : "false");
    else if (key === "current") node.setAttribute("aria-current", value ? "true" : "false");
    else node.setAttribute(key, value);
  }
  for (const child of children) {
    if (child == null) continue;
    node.append(child);
  }
  return node;
}

function fmtMs(value) {
  const number = Number(value);
  return number.toLocaleString("en-US", { maximumFractionDigits: 2 }) + " ms";
}

function fmtInt(value) {
  return Number(value).toLocaleString("en-US");
}

function fmtPct(value) {
  return (Number(value) * 100).toLocaleString("en-US", { maximumFractionDigits: 1, minimumFractionDigits: 1 }) + "%";
}

function fmtMoney(value) {
  const number = Number(value);
  if (!number) return "$0";
  return "$" + number.toLocaleString("en-US", { minimumFractionDigits: 6, maximumFractionDigits: 6 });
}

function rootOf(trace) {
  return trace.spans.find((span) => !span.parent_span_id);
}

function childrenOf(trace) {
  const root = rootOf(trace);
  return trace.spans.filter((span) => span.parent_span_id === root.span_id);
}

function kindOf(span) {
  if (span.status === "ERROR") return "err";
  const op = span.attributes["gen_ai.operation.name"];
  if (op === "chat") return "chat";
  if (op === "execute_tool") return "tool";
  if (span.name.startsWith("state.update")) return "state";
  return "chat";
}

function shortName(span) {
  const phase = span.attributes["houghtonlabs.agent.phase"];
  if (phase) return "chat";
  if (span.attributes["gen_ai.tool.name"]) return span.attributes["gen_ai.tool.name"];
  if (span.attributes["houghtonlabs.state.key"]) return "state " + span.attributes["houghtonlabs.state.key"];
  return span.name.replace("invoke_agent ", "");
}

function traceById(id) {
  return state.traces.traces.find((trace) => trace.task_id === id);
}

function currentView() {
  const hash = decodeURIComponent(location.hash.replace(/^#/, ""));
  if (hash === "metrics" || hash === "slo") return { view: "metrics" };
  const featured = state.traces.featured.failing;
  const id = traceById(hash) ? hash : featured;
  return { view: "trace", id };
}

function selectDefaultSpan(trace) {
  const children = childrenOf(trace);
  return children.find((span) => span.status === "ERROR") || children[children.length - 1];
}

async function load() {
  const app = $("#app");
  try {
    const [traces, metrics] = await Promise.all([
      fetch("data/traces.json").then((response) => {
        if (!response.ok) throw new Error("traces.json " + response.status);
        return response.json();
      }),
      fetch("data/metrics.json").then((response) => {
        if (!response.ok) throw new Error("metrics.json " + response.status);
        return response.json();
      }),
    ]);
    state.traces = traces;
    state.metrics = metrics;
    render();
  } catch (error) {
    app.replaceChildren(
      el("p", {
        class: "error-banner",
        text: "Could not load site/data. Serve this folder (make run) so the browser can fetch the JSON. " + error.message,
      })
    );
  }
}

function render() {
  const view = currentView();
  $("#view-trace").setAttribute("aria-pressed", view.view === "trace" ? "true" : "false");
  $("#view-slo").setAttribute("aria-pressed", view.view === "metrics" ? "true" : "false");
  const app = $("#app");
  app.replaceChildren(view.view === "metrics" ? renderMetrics() : renderTrace(view.id));
}

function renderTrace(taskId) {
  const trace = traceById(taskId);
  const children = childrenOf(trace);
  const root = rootOf(trace);
  if (!state.selectedSpan || !trace.spans.includes(state.selectedSpan)) {
    state.selectedSpan = selectDefaultSpan(trace);
  }
  const failing = state.traces.featured.failing;
  const healthy = state.traces.featured.healthy;
  const featured = [traceById(failing), traceById(healthy)];
  const rest = state.traces.traces.filter((item) => item.task_id !== failing && item.task_id !== healthy);

  return el("div", { class: "workspace" }, [
    el("aside", { class: "picker" }, [
      el("h2", { text: "Runs" }),
      ...featured.map((item) => choiceButton(item, item.task_id === failing ? "Failing run" : "Healthy run")),
      el("h2", { text: "All incidents", style: "margin-top:16px" }),
      el("ul", { class: "runs" }, rest.map((item) => el("li", {}, [runButton(item)]))),
    ]),
    el("section", { class: "stage" }, [
      summary(trace, root),
      trace.story ? el("p", { class: trace.outcome === "error" ? "callout" : "callout calm", text: trace.story }) : null,
      el("div", { class: "legend" }, [
        el("span", {}, [el("i", { class: "swatch chat" }), "LLM"]),
        el("span", {}, [el("i", { class: "swatch tool" }), "Tool"]),
        el("span", {}, [el("i", { class: "swatch state" }), "State"]),
        el("span", {}, [el("i", { class: "swatch err" }), "Error"]),
      ]),
      waterfall(trace, root, children),
    ]),
    el("aside", { class: "detail" }, [detail(trace, children, state.selectedSpan)]),
  ]);
}

function choiceButton(trace, kicker) {
  const bad = trace.outcome === "error";
  return el("button", {
    class: "choice " + (bad ? "bad" : "good"),
    type: "button",
    pressed: currentView().id === trace.task_id,
    onClick: () => openTrace(trace.task_id),
  }, [
    el("span", { class: "kicker", text: kicker }),
    el("strong", { text: trace.task_id }),
    el("em", { text: `${trace.steps} steps · ${bad ? trace.error_type : "ok"}` }),
  ]);
}

function runButton(trace) {
  return el("button", {
    type: "button",
    current: currentView().id === trace.task_id,
    onClick: () => openTrace(trace.task_id),
  }, [
    el("i", { class: "dot" + (trace.outcome === "error" ? " bad" : "") }),
    el("span", { class: "id", text: trace.task_id }),
    el("span", { class: "meta", text: String(trace.steps) }),
  ]);
}

function openTrace(taskId) {
  state.selectedSpan = null;
  location.hash = taskId;
}

function summary(trace, root) {
  const tokens = Number(root.attributes["gen_ai.usage.input_tokens"] || 0) + Number(root.attributes["gen_ai.usage.output_tokens"] || 0);
  const cost = root.attributes["houghtonlabs.cost.usd"];
  const bad = trace.outcome === "error";
  return el("div", { class: "summary" }, [
    el("h1", { text: trace.title }),
    el("span", { class: "pill " + (bad ? "bad" : "good"), text: bad ? "error · " + trace.error_type : "ok" }),
    stat("steps", trace.steps),
    stat("duration", fmtMs(trace.duration_ms)),
    stat("tokens", fmtInt(tokens)),
    stat("cost", fmtMoney(cost)),
    stat("model", root.attributes["gen_ai.request.model"] || ""),
  ]);
}

function stat(label, value) {
  return el("span", { class: "stat" }, [
    document.createTextNode(label + " "),
    el("b", { text: String(value) }),
  ]);
}

function waterfall(trace, root, children) {
  const start = root.start_time_unix_nano;
  const total = Math.max(1, root.end_time_unix_nano - start);
  const rows = [
    barRow(null, "invoke_agent", root, start, total, true),
    ...children.map((span) => barRow(span.attributes["houghtonlabs.step"], shortName(span), span, start, total, false)),
  ];
  return el("div", { class: "waterfall" }, rows);
}

function barRow(step, label, span, origin, total, isRoot) {
  const offset = ((span.start_time_unix_nano - origin) / total) * 100;
  const width = (Number(span.duration_ms) * 1e6 / total) * 100;
  const kind = isRoot ? "root" : kindOf(span);
  const phase = span.attributes["houghtonlabs.agent.phase"];
  return el("button", {
    class: "row",
    type: "button",
    current: state.selectedSpan === span,
    onClick: () => {
      state.selectedSpan = span;
      render();
    },
  }, [
    el("span", { class: "label" }, [
      el("span", { class: "step", text: step == null ? "" : String(step).padStart(2, "0") }),
      el("span", { class: "name", text: label }),
      phase ? el("span", { class: "phase", text: phase }) : null,
      el("span", { class: "ms", text: fmtMs(span.duration_ms) }),
    ]),
    el("span", { class: "track", title: `${span.name} · ${fmtMs(span.duration_ms)}` }, [
      el("span", {
        class: "bar " + kind,
        style: `left:${Math.max(0, offset)}%;width:${Math.max(width, 0)}%`,
      }),
    ]),
  ]);
}

function detail(trace, children, span) {
  if (!span) return el("p", { text: "Select a span." });
  const attrs = span.attributes;
  const bits = [];
  bits.push(el("h2", { text: "Span" }));
  bits.push(el("p", { class: "who", text: span.name }));
  bits.push(el("p", { class: "explain", text: explain(span) }));
  const follow = followOn(children, span);
  if (follow) bits.push(el("p", { class: "explain", text: follow }));
  const quote = attrs["houghtonlabs.llm.output_preview"]
    || attrs["houghtonlabs.tool.result_preview"]
    || attrs["houghtonlabs.tool.error_message"]
    || attrs["houghtonlabs.state.preview"]
    || "";
  if (quote) bits.push(el("p", { class: "quote", text: quote }));
  const rows = Object.keys(attrs).sort(attrOrder).flatMap((key) => [
    el("dt", { text: key }),
    el("dd", { text: formatAttr(attrs[key]) }),
  ]);
  bits.push(el("dl", { class: "attrs" }, rows));
  if (span.status_message && span.status_message !== quote) {
    bits.push(el("p", { class: "note", text: span.status_message }));
  }
  bits.push(el("p", { class: "note", text: "Trace " + trace.trace_id }));
  return el("div", {}, bits);
}

function explain(span) {
  const attrs = span.attributes;
  if (!span.parent_span_id) {
    return span.status === "ERROR"
      ? `Root invoke_agent is ERROR because ${attrs["houghtonlabs.agent.failing_span"]} ended with error.type=${attrs["error.type"]}.`
      : "Root invoke_agent completed. Every child span is OK.";
  }
  if (span.status === "ERROR" && attrs["gen_ai.operation.name"] === "execute_tool") {
    return `${attrs["gen_ai.tool.name"]} failed with error.type=${attrs["error.type"]} after ${fmtMs(span.duration_ms)}. The args hash is on the span; the raw arguments stay off unless content mode is full.`;
  }
  if (attrs["gen_ai.operation.name"] === "chat") {
    const tokens = `${attrs["gen_ai.usage.input_tokens"]} in / ${attrs["gen_ai.usage.output_tokens"]} out`;
    return `${attrs["houghtonlabs.agent.phase"]} hop on ${attrs["gen_ai.request.model"]}. ${tokens}. Illustrative cost ${fmtMoney(attrs["houghtonlabs.cost.usd"])}.`;
  }
  if (attrs["gen_ai.operation.name"] === "execute_tool") {
    const hash = String(attrs["houghtonlabs.tool.args_sha256"] || "");
    return `${attrs["gen_ai.tool.name"]} finished in ${fmtMs(span.duration_ms)}. Args SHA-256 starts ${hash.slice(0, 16)}.`;
  }
  if (attrs["houghtonlabs.state.key"]) {
    return `Scratchpad write ${attrs["houghtonlabs.state.key"]}. Before and after are SHA-256 of the canonical JSON.`;
  }
  return span.name;
}

function followOn(children, span) {
  const index = children.indexOf(span);
  if (index < 0 || span.status !== "ERROR") return "";
  const next = children[index + 1];
  const after = children[index + 2];
  if (!next) return "";
  const second = after ? ` Then ${after.name} is ${after.status}.` : "";
  return `Next span is ${next.name} (${next.status}).${second}`;
}

function attrOrder(a, b) {
  const priority = [
    "error.type",
    "gen_ai.tool.name",
    "gen_ai.operation.name",
    "gen_ai.request.model",
    "houghtonlabs.agent.phase",
    "houghtonlabs.tool.args_sha256",
    "houghtonlabs.tool.args_preview",
    "houghtonlabs.tool.error_message",
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.output_tokens",
    "houghtonlabs.cost.usd",
    "houghtonlabs.step",
  ];
  const ia = priority.indexOf(a);
  const ib = priority.indexOf(b);
  if (ia !== -1 || ib !== -1) return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib);
  const rank = (key) => (key.startsWith("gen_ai.") ? 0 : key.startsWith("server.") ? 1 : 2);
  return rank(a) - rank(b) || a.localeCompare(b);
}

function formatAttr(value) {
  if (Array.isArray(value)) return value.join(", ");
  if (typeof value === "boolean") return value ? "true" : "false";
  return String(value);
}

function renderMetrics() {
  const metrics = state.metrics;
  const tokens = metrics.tokens_per_task;
  const maxP95 = Math.max(...metrics.tools.map((tool) => tool.latency_ms.p95 || 0), 1);
  const cards = [
    ["Task error rate", fmtPct(metrics.task_error_rate), `${metrics.task_error_count} of ${metrics.task_count} runs`],
    ["Tool error rate", fmtPct(metrics.tool_error_rate), `${metrics.tool_error_count} of ${metrics.tool_call_count} calls`],
    ["Tokens / task", fmtInt(tokens.p50), `p50 · p95 ${fmtInt(tokens.p95)}`],
    ["Cost / task", fmtMoney(metrics.cost_usd_per_task.p50), "p50 · illustrative"],
  ];
  return el("section", { class: "metrics" }, [
    el("header", {}, [
      el("h1", { text: "SLOs from the same spans" }),
      el("p", { text: metrics.price_note }),
    ]),
    el("div", { class: "cards" }, cards.map(([label, value, sub]) =>
      el("article", { class: "card" }, [
        el("span", { text: label }),
        el("strong", { text: value }),
        el("em", { text: sub }),
      ])
    )),
    el("h2", { text: "Latency and errors by tool" }),
    el("div", { class: "table-wrap" }, [
      el("table", { class: "slo" }, [
        el("thead", {}, [
          el("tr", {}, ["Tool", "Calls", "Errors", "Error rate", "p50", "p95"].map((label) => el("th", { text: label }))),
        ]),
        el("tbody", {}, metrics.tools.map((tool) => {
          const width = Math.max(2, (tool.latency_ms.p95 / maxP95) * 88);
          return el("tr", {}, [
            el("td", { text: tool.name }),
            el("td", { text: String(tool.calls) }),
            el("td", { text: String(tool.errors) }),
            el("td", {}, [
              el("i", { class: "barline" + (tool.errors ? " bad" : ""), style: `width:${Math.max(tool.error_rate * 70, tool.errors ? 6 : 2)}px` }),
              document.createTextNode(fmtPct(tool.error_rate)),
            ]),
            el("td", { text: fmtMs(tool.latency_ms.p50) }),
            el("td", {}, [
              el("i", { class: "barline", style: `width:${width}px` }),
              document.createTextNode(fmtMs(tool.latency_ms.p95)),
            ]),
          ]);
        })),
      ]),
    ]),
    el("p", {
      class: "note",
      text: `${metrics.task_count} synthetic incidents. Percentiles use linear interpolation: rank k = (n − 1) × p, then interpolate. p95 on sensor.status is the injected timeout, which is the point.`,
    }),
  ]);
}

$("#view-trace").addEventListener("click", () => {
  const failing = state.traces ? state.traces.featured.failing : "inc-1042";
  location.hash = failing;
});
$("#view-slo").addEventListener("click", () => {
  location.hash = "metrics";
});
window.addEventListener("hashchange", () => {
  state.selectedSpan = null;
  if (state.traces) render();
});

load();
