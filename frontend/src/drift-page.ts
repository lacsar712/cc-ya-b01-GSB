import { css, html, LitElement } from "lit";
import { customElement, property, state } from "lit/decorators.js";

type DriftSample = {
  processed_at: string;
  yaw_err_deg: number;
};

type DriftTurbine = {
  turbine_code: string;
  slope_deg_per_hour: number | null;
  sample_count: number;
  samples: DriftSample[];
};

type DriftResponse = {
  window_minutes: number;
  generated_at: string;
  turbines: DriftTurbine[];
};

const POLL_MS = 2500;
const REFETCH_DEBOUNCE_MS = 300;

@customElement("yaw-drift-page")
export class YawDriftPage extends LitElement {
  static styles = css`
    section {
      background: #1e293b;
      border-radius: 8px;
      padding: 1rem 1.25rem;
      margin-bottom: 1rem;
      border: 1px solid #334155;
    }
    h2 {
      margin: 0 0 0.75rem;
      font-size: 1.1rem;
    }
    .slider-row {
      display: flex;
      align-items: center;
      gap: 1rem;
      flex-wrap: wrap;
    }
    input[type="range"] {
      flex: 1;
      min-width: 220px;
      accent-color: #38bdf8;
    }
    input[type="range"]:disabled {
      accent-color: #475569;
      cursor: not-allowed;
    }
    .window-readout {
      font-variant-numeric: tabular-nums;
      font-weight: 600;
      color: #38bdf8;
      white-space: nowrap;
    }
    .hint {
      color: #94a3b8;
      font-size: 0.85rem;
      margin: 0.5rem 0 0;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.9rem;
    }
    th,
    td {
      text-align: left;
      padding: 0.5rem 0.4rem;
      border-bottom: 1px solid #334155;
      vertical-align: top;
    }
    th {
      color: #94a3b8;
      font-weight: 600;
    }
    .slope {
      font-variant-numeric: tabular-nums;
      font-weight: 700;
    }
    .slope.pos {
      color: #fca5a5;
    }
    .slope.neg {
      color: #86efac;
    }
    .slope.flat,
    .slope.none {
      color: #94a3b8;
    }
    .samples {
      display: flex;
      flex-wrap: wrap;
      gap: 0.3rem;
    }
    .sample-chip {
      background: #0f172a;
      border: 1px solid #334155;
      border-radius: 4px;
      padding: 0.1rem 0.4rem;
      font-size: 0.78rem;
      color: #cbd5e1;
      white-space: nowrap;
    }
    .empty {
      color: #64748b;
    }
    .spec {
      margin: 0;
      padding-left: 1.2rem;
      color: #cbd5e1;
      font-size: 0.88rem;
      line-height: 1.7;
    }
    .err {
      color: #f87171;
      margin-top: 0.5rem;
    }
  `;

  /** 登录令牌与角色由父组件传入；斜率数字一律来自后台拟合结果。 */
  @property({ attribute: false }) token = "";
  @property({ attribute: false }) role = "reader";

  @state() private windowMinutes = 60;
  @state() private data: DriftResponse | null = null;
  @state() private error = "";

  private _pollTimer?: number;
  private _debounceTimer?: number;

  connectedCallback() {
    super.connectedCallback();
    void this.fetchSlope();
    this._pollTimer = window.setInterval(() => void this.fetchSlope(), POLL_MS);
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    if (this._pollTimer) clearInterval(this._pollTimer);
    if (this._debounceTimer) clearTimeout(this._debounceTimer);
  }

  private get canTuneWindow() {
    return this.role === "writer";
  }

  private async fetchSlope() {
    if (!this.token) return;
    try {
      const res = await fetch(
        `/api/drift/slope?window_minutes=${this.windowMinutes}`,
        { headers: { Authorization: `Bearer ${this.token}` } }
      );
      if (!res.ok) {
        this.error = "斜率接口请求失败";
        return;
      }
      this.data = (await res.json()) as DriftResponse;
      this.error = "";
    } catch {
      /* 忽略瞬时网络异常，下轮轮询再试 */
    }
  }

  private onWindowInput(e: Event) {
    if (!this.canTuneWindow) return;
    this.windowMinutes = Number((e.target as HTMLInputElement).value);
    if (this._debounceTimer) clearTimeout(this._debounceTimer);
    this._debounceTimer = window.setTimeout(
      () => void this.fetchSlope(),
      REFETCH_DEBOUNCE_MS
    );
  }

  private onWindowChange() {
    if (!this.canTuneWindow) return;
    if (this._debounceTimer) clearTimeout(this._debounceTimer);
    void this.fetchSlope();
  }

  private fmtSlope(slope: number | null): string {
    if (slope === null || slope === undefined) return "—";
    const v = Math.abs(slope) < 0.005 ? 0 : slope;
    return `${v > 0 ? "+" : ""}${v.toFixed(2)}`;
  }

  private slopeClass(slope: number | null): string {
    if (slope === null || slope === undefined) return "none";
    if (Math.abs(slope) < 0.005) return "flat";
    return slope > 0 ? "pos" : "neg";
  }

  private fmtTime(iso: string): string {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    const pad = (n: number) => String(n).padStart(2, "0");
    return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(
      d.getMinutes()
    )}:${pad(d.getSeconds())}`;
  }

  private renderWindowZone() {
    return html`
      <section>
        <h2>统计窗宽</h2>
        <div class="slider-row">
          <input
            type="range"
            min="0"
            max="240"
            step="5"
            .value=${String(this.windowMinutes)}
            ?disabled=${!this.canTuneWindow}
            @input=${this.onWindowInput}
            @change=${this.onWindowChange}
          />
          <span class="window-readout">窗宽：${this.windowMinutes} 分钟</span>
        </div>
        <p class="hint">
          ${this.canTuneWindow
            ? "拖动滑块调整统计窗宽，松手后按新窗口重新拟合。"
            : "观察员只读：不可调整窗宽，也不可提交记录。"}
        </p>
      </section>
    `;
  }

  private renderTurbineZone() {
    const turbines = this.data?.turbines ?? [];
    return html`
      <section>
        <h2>逐机拟合斜率</h2>
        <table>
          <thead>
            <tr>
              <th>机组</th>
              <th>拟合斜率（°/h）</th>
              <th>样本点数</th>
              <th>窗内样本点（办结时刻 → 误差°）</th>
            </tr>
          </thead>
          <tbody>
            ${turbines.length === 0
              ? html`<tr>
                  <td colspan="4" class="empty">窗内暂无已办结样本</td>
                </tr>`
              : turbines.map(
                  (t) => html`
                    <tr>
                      <td>${t.turbine_code}</td>
                      <td>
                        <span class="slope ${this.slopeClass(t.slope_deg_per_hour)}">
                          ${this.fmtSlope(t.slope_deg_per_hour)}
                        </span>
                      </td>
                      <td>${t.sample_count}</td>
                      <td>
                        ${t.samples.length === 0
                          ? html`<span class="empty">窗内无样本</span>`
                          : html`<span class="samples">
                              ${t.samples.map(
                                (s) => html`<span class="sample-chip"
                                  >${this.fmtTime(s.processed_at)} →
                                  ${s.yaw_err_deg}°</span
                                >`
                              )}
                            </span>`}
                      </td>
                    </tr>
                  `
                )}
          </tbody>
        </table>
        ${this.error ? html`<p class="err">${this.error}</p>` : null}
      </section>
    `;
  }

  private renderSpecZone() {
    return html`
      <section>
        <h2>口径说明（只读）</h2>
        <ul class="spec">
          <li>样本：仅取「已完成」记录，按办结时刻（processed_at）落入统计窗的点入样本；待处理记录不入样本。</li>
          <li>拟合：后台以办结时刻为横轴、偏航误差为纵轴做最小二乘直线拟合，斜率单位为度/小时。</li>
          <li>斜率由后台计算，前端只展示，不在页面侧改数或重算。</li>
          <li>窗内样本不足 2 点（含窗口缩到样本之外）时斜率为空，显示「—」。</li>
          <li>斜率为正表示误差随时间抬升，为负表示回落，接近 0 视为平稳。</li>
          <li>窗宽仅技师可拖调；观察员只读，不可调整窗宽、不可提交记录。</li>
        </ul>
      </section>
    `;
  }

  render() {
    return html`
      ${this.renderWindowZone()} ${this.renderTurbineZone()}
      ${this.renderSpecZone()}
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    "yaw-drift-page": YawDriftPage;
  }
}
