"""Minimal local console HTML for operating the pilot end to end."""

from __future__ import annotations


def render_console_html(default_data_room: str) -> str:
    """Return the minimal console HTML with inline JavaScript."""

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Angelic Pilot 操作台</title>
  <style>
    :root {{
      --bg: #f6f3ea;
      --panel: #fffdf8;
      --ink: #182028;
      --muted: #59636e;
      --line: #d9d1c0;
      --accent: #234b6b;
      --accent-soft: #dce9f3;
      --ok: #1c6b44;
      --warn: #9c5b13;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Georgia, "Times New Roman", serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, #efe8d7 0, transparent 28rem),
        linear-gradient(180deg, #f9f6ee 0%, var(--bg) 100%);
    }}
    .shell {{
      max-width: 1200px;
      margin: 0 auto;
      padding: 32px 20px 48px;
    }}
    h1 {{
      margin: 0 0 8px;
      font-size: 2.2rem;
    }}
    .sub {{
      margin: 0 0 24px;
      color: var(--muted);
      max-width: 72ch;
      line-height: 1.5;
    }}
    .grid {{
      display: grid;
      grid-template-columns: 1.1fr 0.9fr;
      gap: 20px;
    }}
    .card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 20px;
      box-shadow: 0 12px 30px rgba(24, 32, 40, 0.06);
    }}
    .card h2 {{
      margin-top: 0;
      margin-bottom: 14px;
      font-size: 1.25rem;
    }}
    label {{
      display: block;
      margin: 12px 0 6px;
      font-size: 0.95rem;
      color: var(--muted);
    }}
    input,
    select {{
      width: 100%;
      padding: 12px 14px;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: #fff;
      color: var(--ink);
      font: inherit;
    }}
    .actions {{
      display: flex;
      gap: 12px;
      margin-top: 18px;
      flex-wrap: wrap;
    }}
    button {{
      border: 0;
      border-radius: 999px;
      padding: 11px 18px;
      font: inherit;
      cursor: pointer;
    }}
    .primary {{
      background: var(--accent);
      color: white;
    }}
    .secondary {{
      background: var(--accent-soft);
      color: var(--accent);
    }}
    .status {{
      min-height: 24px;
      margin-top: 12px;
      color: var(--muted);
    }}
    .status.ok {{ color: var(--ok); }}
    .status.warn {{ color: var(--warn); }}
    pre {{
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      background: #faf7f0;
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 14px;
      font-size: 0.9rem;
      line-height: 1.45;
    }}
    .runs {{
      display: grid;
      gap: 12px;
    }}
    .run {{
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 14px;
      background: #fff;
    }}
    .run-head {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
      margin-bottom: 10px;
    }}
    .run-id {{
      font-weight: 700;
    }}
    .links {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 10px;
    }}
    a {{
      color: var(--accent);
      text-decoration: none;
    }}
    a:hover {{ text-decoration: underline; }}
    .mono {{
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 0.9rem;
    }}
    @media (max-width: 920px) {{
      .grid {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <div class="shell">
    <h1>Angelic Pilot 操作台</h1>
    <p class="sub">
      这是一个最基本的本地控制台。你可以填写数据室路径、触发一次 P&amp;L 生成流程、
      查看最近运行记录，并直接下载工作簿、抽取 JSON 和校验报告。
    </p>

    <div class="grid">
      <section class="card">
        <h2>运行一次</h2>
        <form id="run-form">
          <label for="data_room_dir">数据室目录</label>
          <input id="data_room_dir" name="data_room_dir" value="{default_data_room}" />

          <label for="run_label">运行名称（可选）</label>
          <input id="run_label" name="run_label" placeholder="例如：某项目数据室批次" />

          <label for="extraction_backend">抽取后端</label>
          <select id="extraction_backend" name="extraction_backend">
            <option value="deterministic">deterministic</option>
            <option value="gemini">gemini</option>
          </select>

          <label for="template_workbook_path">模板工作簿路径（可选）</label>
          <input id="template_workbook_path" name="template_workbook_path" placeholder="/absolute/path/to/template.xlsx" />

          <label for="ai_workbook_path">AI 工作簿路径（可选）</label>
          <input id="ai_workbook_path" name="ai_workbook_path" placeholder="/absolute/path/to/ai_workbook.xlsx" />

          <label for="gold_workbook_path">Gold 工作簿路径（可选）</label>
          <input id="gold_workbook_path" name="gold_workbook_path" placeholder="/absolute/path/to/gold_standard.xlsx" />

          <label for="output_root">输出目录（可选）</label>
          <input id="output_root" name="output_root" placeholder="outputs" />

          <div class="actions">
            <button class="primary" type="submit">运行系统</button>
            <button class="secondary" type="button" id="use-sample">填入示例路径</button>
            <button class="secondary" type="button" id="refresh-runs">刷新运行记录</button>
          </div>
        </form>
        <div id="status" class="status"></div>
      </section>

      <section class="card">
        <h2>最近一次返回</h2>
        <pre id="result">等待运行…</pre>
      </section>
    </div>

    <section class="card" style="margin-top: 20px;">
      <h2>最近运行记录</h2>
      <div id="runs" class="runs"></div>
    </section>
  </div>

  <script>
    const samplePath = {default_data_room!r};
    const statusEl = document.getElementById("status");
    const resultEl = document.getElementById("result");
    const runsEl = document.getElementById("runs");

    function escapeHtml(value) {{
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;");
    }}

    function optionalValue(id) {{
      const value = document.getElementById(id).value.trim();
      return value === "" ? null : value;
    }}

    function renderRun(run) {{
      const artifactLinks = Object.keys(run.artifact_paths || {{}})
        .map((key) => `<a href="/runs/${{encodeURIComponent(run.run_id)}}/artifacts/${{encodeURIComponent(key)}}" target="_blank">${{escapeHtml(key)}}</a>`)
        .join("");
      const notes = (run.notes || []).map((note) => `<div>${{escapeHtml(note)}}</div>`).join("");
      const dataRoom = run.input_paths?.data_room_dir ? `<div class="mono">${{escapeHtml(run.input_paths.data_room_dir)}}</div>` : "";
      const backend = run.extraction_backend ? `<div class="mono">backend: ${{escapeHtml(run.extraction_backend)}}</div>` : "";

      return `
        <article class="run">
          <div class="run-head">
            <div>
              <div class="run-id">${{escapeHtml(run.run_id)}}</div>
              <div class="mono">${{escapeHtml(run.created_at)}} | ${{escapeHtml(run.status)}}</div>
            </div>
            <div>${{backend}}${{dataRoom}}</div>
          </div>
          <div>${{notes || "无额外备注"}}</div>
          <div class="links">${{artifactLinks || "<span>暂无工件链接</span>"}}</div>
        </article>
      `;
    }}

    async function loadRuns() {{
      const response = await fetch("/runs");
      const runs = await response.json();
      if (!Array.isArray(runs) || runs.length === 0) {{
        runsEl.innerHTML = "<div class='run'>暂无运行记录。</div>";
        return;
      }}
      runsEl.innerHTML = runs.map(renderRun).join("");
    }}

    document.getElementById("run-form").addEventListener("submit", async (event) => {{
      event.preventDefault();
      statusEl.textContent = "系统运行中，请稍候…";
      statusEl.className = "status";

      const payload = {{
        data_room_dir: document.getElementById("data_room_dir").value.trim(),
        extraction_backend: document.getElementById("extraction_backend").value,
        run_label: optionalValue("run_label"),
        template_workbook_path: optionalValue("template_workbook_path"),
        ai_workbook_path: optionalValue("ai_workbook_path"),
        gold_workbook_path: optionalValue("gold_workbook_path"),
        output_root: optionalValue("output_root"),
      }};

      try {{
        const response = await fetch("/runs", {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify(payload),
        }});
        const data = await response.json();
        resultEl.textContent = JSON.stringify(data, null, 2);
        if (!response.ok) {{
          statusEl.textContent = data.detail || "运行失败";
          statusEl.className = "status warn";
          return;
        }}
        statusEl.textContent = "运行完成，已刷新运行记录。";
        statusEl.className = "status ok";
        await loadRuns();
      }} catch (error) {{
        statusEl.textContent = `请求失败: ${{error}}`;
        statusEl.className = "status warn";
      }}
    }});

    document.getElementById("use-sample").addEventListener("click", () => {{
      document.getElementById("data_room_dir").value = samplePath;
    }});

    document.getElementById("refresh-runs").addEventListener("click", () => {{
      loadRuns();
    }});

    loadRuns();
  </script>
</body>
</html>"""
