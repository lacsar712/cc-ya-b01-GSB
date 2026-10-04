import { css, html, LitElement } from "lit";
import { customElement, property, state } from "lit/decorators.js";

export type Session = {
  token: string;
  username: string;
  role: string;
};

type DriftSample = {
  log_id: number;
  processed_at: string;
  yaw_err_deg: number;
};

type DriftRow = {
  turbine_code: string;
  slope_deg_per_hour: number | null;
  n_samples: number;
  samples: DriftSample[];
  window_seconds: number;
  window_start: string;
  window_end: string;
  fitted_at: string;
};

type DriftSnapshot = {
  window_seconds: number;
  window_start: string;
  window_end: string;
  fitted_at: string;
  slopes: DriftRow[];
};

// |斜率|（度/小时）小于该值记为「平」；纯水平点 OLS 结果在 0 附近的浮点残差也归入平。
const FLAT_EPS = 0.001;
const HOUR = 3600;
const MAX_HOURS = 24 * 7;

export function formatWindow(seconds: number): string {
  if (seconds === 0) return "空窗（0 秒）";
  if (seconds < HOUR) return `${seconds} 秒`;
  const hours = seconds / HOUR;
  if (Number.isInteger(hours)) {
    if (hours % 24 === 0) return `${hours / 24} 天`;
    return `${hours} 小时`;
  }
  return `${seconds} 秒`;
}

function fmtTime(iso: string): string {
  const d = new Date(iso);
  return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, "0")}:${String(
    d.getMinutes()
  ).padStart(2, "0")}`;
}

function classify(row: DriftRow): { label: string; cls: string } {
  if (row.slope_deg_per_hour === null) {
    const label =
      row.n_samples === 0
        ? "空·窗外无点"
        : row.n_samples === 1
          ? "空·不足两点"
          : "空·时刻重合";
    return { label, cls: "empty" };
  }
  const s = row.slope_deg_per_hour;
  if (Math.abs(s) < FLAT_EPS) return { label: "平", cls: "flat" };
  return s > 0 ? { label: "正漂 ↑", cls: "pos" } : { label: "负漂 ↓", cls: "neg" };
}

@customElement("drift-page")
export class DriftPage extends LitElement {
  static styles = css`
    .zone-tag {
      display: inline-block;
      font-size: 0.72rem;
      color: #94a3b8;
      border: 1px solid #475569;
      border-radius: 4px;
      padding: 0.05rem 0.35rem;
      margin-right: 0.4rem;
      vertical-align: middle;
    }
    h2 {
      margin: 0 0 0.75rem;
      font-size: 1.05rem;
    }
    .window-row {
      display: flex;
      align-items: center;
      gap: 0.75rem;
      flex-wrap: wrap;
    }
    input[type="range"] {
      flex: 1;
      min-width: 220px;
      accent-color: #38bdf8;
    }
    input[type="range"]:disabled {
      accent-color: #64748b;
      cursor: not-allowed;
    }
    .preset {
      background: #334155;
      color: #e2e8f0;
      padding: 0.3rem 0.6rem;
      font-size: 0.82rem;
      font-weight: 500;
    }
    .preset.active {
      background: #0369a1;
    }
    .fit-btn {
      background: #0284c7;
    }
    .muted {
      color: #94a3b8;
      font-size: 0.82rem;
    }
    .hint {
      color: #cbd5e1;
      font-size: 0.82rem;
      margin-top: 0.55rem;
    }
    .err {
      color: #f87171;
      margin-top: 0.5rem;
      font-size: 0.85rem;
    }
    .meta {
      color: #94a3b8;
      font-size: 0.8rem;
      margin-top: 0.6rem;
    }
    .turbine {
      border: 1px solid #334155;
      border-radius: 8px;
      padding: 0.7rem 0.9rem;
      margin-bottom: 0.7rem;
      background: #0f172a;
    }
    .turbine-head {
      display: flex;
      align-items: center;
      gap: 0.75rem;
      flex-wrap: wrap;
    }
    .code {
      font-weight: 700;
      font-size: 1.05rem;
      min-width: 3.2rem;
    }
    .slope {
      font-variant-numeric: tabular-nums;
      font-weight: 600;
    }
    .slope.pos {
      color: #fca5a5;
    }
    .slope.neg {
      color: #7dd3fc;
    }
    .slope.flat {
      color: #86efac;
    }
    .slope.empty {
      color: #94a3b8;
    }
    .tag {
      display: inline-block;
      padding: 0.15rem 0.5rem;
      border-radius: 4px;
      font-size: 0.78rem;
      font-weight: 600;
    }
    .tag.pos {
      background: #7f1d1d;
      color: #fecaca;
    }
    .tag.neg {
      background: #0c4a6e;
      color: #bae6fd;
    }
    .tag.flat {
      background: #14532d;
      color: #86efac;
    }
    .tag.empty {
      background: #475569;
      color: #cbd5e1;
    }
    .samples {
      margin: 0.55rem 0 0;
      padding-left: 0.4rem;
      color: #94a3b8;
      font-size: 0.8rem;
      font-variant-numeric: tabular-nums;
    }
    .samples b {
      color: #cbd5e1;
      font-weight: 600;
    }
    .empty-note {
      color: #64748b;
      font-size: 0.9rem;
      padding: 0.4rem 0;
    }
    ol.caliber {
      margin: 0;
      padding-left: 1.2rem;
      color: #cbd5e1;
      font-size: 0.85rem;
      line-height: 1.7;
    }
  `;

  @property({ attribute: false }) session: Session | null = null;
  @property({ type: Boolean }) active = false;
  @property({ attribute: false }) onUnauthorized: (() => void) | null = null;

  @state() private snap: DriftSnapshot | null = null;
  @state() private pendingHours = 24;
  @state() private loaded = false;
  @state() private error = "";
  @state() private fitting = false;

  // 技师本地拖动窗宽后置脏，期间轮询不得用后台窗宽覆盖其待提交选择
  private dirty = false;
  private _pollTimer?: number;

  private get isWriter() {
    return this.session?.role === "writer";
  }

  connectedCallback() {
    super.connectedCallback();
    if (this.active) this._startPolling();
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    this._stopPolling();
  }

  updated(changed: Map<string, unknown>) {
    if (changed.has("active")) {
      if (this.active) this._startPolling();
      else this._stopPolling();
    }
  }

  private _startPolling() {
    if (this._pollTimer !== undefined) return;
    void this.refresh();
    this._pollTimer = window.setInterval(() => void this.refresh(), 2000);
  }

  private _stopPolling() {
    if (this._pollTimer !== undefined) {
      clearInterval(this._pollTimer);
      this._pollTimer = undefined;
    }
  }

  private authHeaders(): HeadersInit {
    return this.session
      ? { Authorization: `Bearer ${this.session.token}` }
      : {};
  }

  private async refresh() {
    if (!this.session) return;
    try {
      const res = await fetch("/api/drift/slopes", {
        headers: this.authHeaders(),
      });
      if (res.status === 401) {
        this.onUnauthorized?.();
        return;
      }
      if (res.status === 409) {
        this.loaded = true;
        return;
      }
      if (!res.ok) return;
      const data = (await res.json()) as DriftSnapshot;
      this.snap = data;
      this.loaded = true;
      // 未本地拖动待拟合窗宽时，滑块跟随后台当前窗宽
      if (!this.dirty && !this.fitting) {
        this.pendingHours = data.window_seconds / HOUR;
      }
    } catch {
      /* 瞬时网络错误忽略，下一轮轮询覆盖 */
    }
  }

  private onSlide(e: Event) {
    // 观察员滑块本身禁用；技师拖动只改本地待拟合窗宽，不写任何数字到后台
    this.dirty = true;
    this.pendingHours = Number((e.target as HTMLInputElement).value);
  }

  private async refit(hours?: number) {
    if (!this.isWriter) return;
    if (hours !== undefined) this.pendingHours = hours;
    const windowSeconds = Math.round(this.pendingHours) * HOUR;
    this.error = "";
    this.fitting = true;
    try {
      const res = await fetch("/api/drift/refit", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...this.authHeaders(),
        },
        body: JSON.stringify({ window_seconds: windowSeconds }),
      });
      const data = await res.json();
      if (res.status === 401) {
        this.onUnauthorized?.();
        return;
      }
      if (!res.ok) {
        this.error = data.detail || "后台拟合失败";
        return;
      }
      this.snap = data as DriftSnapshot;
      this.pendingHours = data.window_seconds / HOUR;
      this.dirty = false;
      this.loaded = true;
    } catch {
      this.error = "拟合请求网络异常";
    } finally {
      this.fitting = false;
    }
  }

  private renderSlope(row: DriftRow) {
    const c = classify(row);
    const slopeText =
      row.slope_deg_per_hour === null
        ? "—"
        : `${row.slope_deg_per_hour >= 0 ? "+" : ""}${row.slope_deg_per_hour.toFixed(4)}`;
    return html`
      <div class="turbine">
        <div class="turbine-head">
          <span class="code">${row.turbine_code}</span>
          <span class="tag ${c.cls}">${c.label}</span>
          <span class="slope ${c.cls}">${slopeText} °/h</span>
          <span class="muted">窗内样本 ${row.n_samples} 点</span>
        </div>
        ${row.samples.length
          ? html`
              <div class="samples">
                ${row.samples.map(
                  (s, i) =>
                    html`<b>${fmtTime(s.processed_at)}</b>
                      ${s.yaw_err_deg}°${i < row.samples.length - 1 ? "　·　" : ""}`
                )}
              </div>
            `
          : html`<div class="samples">窗内无办结点（窗口外不计入）</div>`}
      </div>
    `;
  }

  render() {
    const appliedSeconds = this.snap ? this.snap.window_seconds : null;
    const pendingSeconds = Math.round(this.pendingHours) * HOUR;

    return html`
      <!-- 上区：拖窗宽 + 后台拟合 -->
      <section>
        <h2><span class="zone-tag">上区</span>拟合窗宽</h2>
        <div class="window-row">
          <input
            type="range"
            min="0"
            max="${String(MAX_HOURS)}"
            step="1"
            .value=${String(this.pendingHours)}
            ?disabled=${!this.isWriter || this.fitting}
            @input=${this.onSlide}
            aria-label="拟合窗宽（小时，0 为空窗）"
          />
          <span class="muted" style="min-width:7.5rem;">
            ${this.isWriter ? "拖到：" : "当前窗宽："}${formatWindow(pendingSeconds)}
          </span>
          ${this.isWriter
            ? html`
                <button
                  class="fit-btn"
                  ?disabled=${this.fitting}
                  @click=${() => void this.refit()}
                >
                  ${this.fitting ? "后台拟合中…" : "按此窗宽后台拟合"}
                </button>
              `
            : html`<span class="muted">观察员只读：禁改窗宽、禁填报</span>`}
        </div>
        ${this.isWriter
          ? html`
              <div class="window-row" style="margin-top:0.6rem;">
                ${[0, 6, 24, 168].map(
                  (h) => html`
                    <button
                      class="preset ${this.pendingHours === h ? "active" : ""}"
                      ?disabled=${this.fitting}
                      @click=${() => void this.refit(h)}
                    >
                      ${formatWindow(h * HOUR)}
                    </button>
                  `
                )}
              </div>
              <div class="hint">
                拖动或点预设只是选定窗宽；斜率必须点「后台拟合」由后台重算，页面不接受手填数字。
                ${appliedSeconds !== null
                  ? html`当前已拟合窗宽：<b>${formatWindow(appliedSeconds)}</b>${
                      appliedSeconds !== pendingSeconds
                        ? html`（与待拟合窗宽 ${formatWindow(pendingSeconds)} 不一致）`
                        : null
                    }`
                  : null}
              </div>
            `
          : null}
        ${this.error ? html`<p class="err">${this.error}</p>` : null}
        ${this.snap
          ? html`<p class="meta">
              最近拟合 ${fmtTime(this.snap.fitted_at)} ｜ 窗区间
              ${fmtTime(this.snap.window_start)} ～ ${fmtTime(this.snap.window_end)}
            </p>`
          : null}
      </section>

      <!-- 中区：逐机拟合斜率与样本点 -->
      <section>
        <h2><span class="zone-tag">中区</span>逐机漂移斜率与样本点</h2>
        ${!this.loaded
          ? html`<p class="muted">加载中…</p>`
          : !this.snap
            ? html`<p class="empty-note">尚未进行后台拟合。</p>`
            : this.snap.slopes.length === 0
              ? html`<p class="empty-note">还没有任何已办结记录，无机组可拟合。</p>`
              : this.snap.slopes.map((row) => this.renderSlope(row))}
      </section>

      <!-- 下区：只读口径说明 -->
      <section>
        <h2><span class="zone-tag">下区</span>口径说明（只读）</h2>
        <ol class="caliber">
          <li>样本只取<b>已办结</b>记录，沿<b>办结时刻</b>串点；待处理（未办结）的点不入样本。</li>
          <li>每个机组单独做最小二乘直线拟合，x 为办结时刻（小时），斜率单位 <b>度/小时</b>。</li>
          <li>窗为 [当前时刻 − 窗宽, 当前时刻]；窗内不足 2 点、窗内一个点都没有，或窗内各点办结时刻重合（无法成线）时斜率记为<b>空</b>。</li>
          <li>
            斜率为正＝偏航误差在抬升（正漂），为负＝回落（负漂）；|斜率| &lt;
            ${FLAT_EPS} 度/小时记为<b>平</b>。
          </li>
          <li>窗宽仅现场技师可调整，且改动只在后台重新拟合后生效；观察员禁改、禁报。</li>
          <li>斜率一律由后台拟合产出，前端不能手工填写或修改斜率数字。</li>
        </ol>
      </section>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    "drift-page": DriftPage;
  }
}
