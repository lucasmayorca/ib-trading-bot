"""
Full dashboard HTML template for the cloud version.
Ported from vista_web.py with cloud-specific adaptations:
- JWT auth header (user email + logout)
- Bridge connection status indicator
- "Conectar TWS" setup tab
- 401 handling on all fetch calls
- Options Lab / Trades History tabs (API endpoints needed on cloud)
"""


def get_dashboard_html():
    return r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>IB Trading Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500;600;700&display=swap');
:root{--bg:#080b12;--surface:#0e1219;--card:#141a24;--border:#1e2736;--border-subtle:#161d28;--accent:#6366f1;--accent-glow:#6366f140;--accent-soft:#818cf830;--buy:#10b981;--sell:#ef4444;--hold:#f59e0b;--text:#e8ecf2;--muted:#7c8898;--dim:#4b5668;--radius:10px;--radius-lg:14px;--shadow-sm:0 1px 2px rgba(0,0,0,.3);--shadow-md:0 4px 12px rgba(0,0,0,.25);--shadow-lg:0 8px 32px rgba(0,0,0,.35);--glass:rgba(255,255,255,.03);--glass-border:rgba(255,255,255,.06)}
*{margin:0;padding:0;box-sizing:border-box}
html{font-size:14px;-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale;text-rendering:optimizeLegibility}
body{background:var(--bg);color:var(--text);font-family:'Inter',system-ui,-apple-system,sans-serif;font-size:14px;line-height:1.5;overflow-x:hidden}
::selection{background:var(--accent);color:#fff}
::-webkit-scrollbar{width:6px;height:6px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}
::-webkit-scrollbar-thumb:hover{background:var(--dim)}

/* === HEADER === */
.header{background:var(--surface);padding:18px 32px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;backdrop-filter:blur(12px)}
.header h1{font-size:17px;color:#fff;font-weight:800;letter-spacing:-.3px}
.header h1 em{font-style:normal;color:var(--accent);font-weight:800}
.header .sub{color:var(--muted);font-size:12px;text-align:right;font-weight:500}
.header-right{display:flex;align-items:center;gap:14px}
.bridge-status{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--muted)}
.bridge-dot{width:8px;height:8px;border-radius:50%;background:var(--dim)}
.bridge-dot.on{background:var(--buy);box-shadow:0 0 8px rgba(16,185,129,.4)}
.bridge-dot.off{background:var(--sell)}
.user-email{color:var(--muted);font-size:11px;max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.btn-logout{background:rgba(255,255,255,.06);color:var(--muted);border:1px solid var(--border);padding:5px 12px;border-radius:6px;font-size:11px;font-weight:600;cursor:pointer;text-decoration:none;transition:all .15s}
.btn-logout:hover{color:var(--text);border-color:var(--accent)}

/* === COUNTERS === */
.counters{display:flex;gap:8px;padding:12px 32px;background:var(--bg);border-bottom:1px solid var(--border);flex-wrap:wrap;align-items:center}
.counter{padding:5px 14px;border-radius:8px;font-weight:700;font-size:12px;letter-spacing:.3px;border:1px solid transparent;transition:all .15s;cursor:default}
.c-buy{background:rgba(16,185,129,.1);color:var(--buy);border-color:rgba(16,185,129,.2)}
.c-sell{background:rgba(239,68,68,.1);color:var(--sell);border-color:rgba(239,68,68,.2)}
.c-hold{background:rgba(245,158,11,.1);color:var(--hold);border-color:rgba(245,158,11,.2)}
.c-nodata{background:rgba(75,86,104,.15);color:var(--muted);border-color:var(--border)}
.c-total{background:rgba(99,102,241,.1);color:#a5b4fc;border-color:rgba(99,102,241,.2)}
.c-buy-near{background:rgba(16,185,129,.08);color:#6ee7b7;border-color:rgba(16,185,129,.15)}
.c-sell-near{background:rgba(239,68,68,.08);color:#fca5a5;border-color:rgba(239,68,68,.15)}
.c-turning-buy{background:rgba(134,239,172,.06);color:#86efac;border-color:rgba(134,239,172,.15)}
.c-turning-sell{background:rgba(253,164,175,.06);color:#fda4af;border-color:rgba(253,164,175,.15)}
.c-zone{background:rgba(125,211,252,.06);color:#7dd3fc;border-color:rgba(125,211,252,.15)}
.c-neutral{background:rgba(148,163,184,.08);color:#94a3b8;border-color:rgba(148,163,184,.2)}

/* === GRID TABLE === */
.content{padding:0 32px 20px;overflow-x:auto}
.list-header,.stock-row{
  display:grid;
  grid-template-columns:20px 70px 84px 90px 40px 52px 52px 52px 52px 52px 60px 48px 60px 40px 44px 56px 56px;
  gap:4px;align-items:center;padding:8px 14px;min-width:960px;
}
.list-header{
  background:var(--surface);
  border-bottom:1px solid var(--accent);color:var(--muted);
  font-size:10px;text-transform:uppercase;letter-spacing:.7px;font-weight:700;
  position:sticky;top:0;z-index:10;border-radius:8px 8px 0 0;
}
.list-header .sep{border-left:1px solid var(--dim);padding-left:8px}

/* === ACCORDION === */
details{background:var(--surface);border-bottom:1px solid var(--border-subtle);margin:0;transition:all .2s ease}
details:first-child{border-top:1px solid var(--border-subtle)}
details[open]{background:var(--card);border-color:rgba(99,102,241,.15)}
details[open]+details{border-top:1px solid rgba(99,102,241,.15)}
summary{cursor:pointer;list-style:none;transition:background .15s}
summary::-webkit-details-marker{display:none}
summary:hover{background:rgba(255,255,255,.025)}
.arrow{color:var(--dim);font-size:9px;transition:transform .2s ease;text-align:center}
details[open] .arrow{transform:rotate(90deg);color:var(--accent)}

/* === CELLS === */
.sym{font-weight:800;color:#fff;font-size:14px;letter-spacing:-.3px}
.price{font-family:'JetBrains Mono',monospace;font-weight:600;color:#cbd5e1;text-align:right;font-size:13px}
.badge{display:inline-block;padding:3px 10px;border-radius:6px;font-weight:800;font-size:9.5px;text-transform:uppercase;text-align:center;letter-spacing:.3px;white-space:nowrap;min-width:64px}
.b-buy{background:rgba(16,185,129,.12);color:var(--buy);border:1px solid rgba(16,185,129,.25)}
.b-buy-strong{background:rgba(16,185,129,.18);color:#34d399;border:1px solid rgba(16,185,129,.35);box-shadow:0 0 12px rgba(16,185,129,.15)}
.b-sell{background:rgba(239,68,68,.12);color:var(--sell);border:1px solid rgba(239,68,68,.25)}
.b-sell-strong{background:rgba(239,68,68,.18);color:#f87171;border:1px solid rgba(239,68,68,.35);box-shadow:0 0 12px rgba(239,68,68,.15)}
.b-buy-near{background:rgba(16,185,129,.08);color:#6ee7b7;border:1px solid rgba(16,185,129,.18)}
.b-sell-near{background:rgba(239,68,68,.08);color:#fca5a5;border:1px solid rgba(239,68,68,.18)}
.b-turning-buy{background:rgba(167,243,208,.06);color:#86efac;border:1px solid rgba(134,239,172,.15)}
.b-turning-sell{background:rgba(254,202,202,.06);color:#fda4af;border:1px solid rgba(253,164,175,.15)}
.b-oversold{background:rgba(56,189,248,.06);color:#7dd3fc;border:1px solid rgba(125,211,252,.15)}
.b-overbought{background:rgba(192,132,252,.06);color:#d8b4fe;border:1px solid rgba(216,180,254,.15)}
.b-hold{background:rgba(245,158,11,.1);color:var(--hold);border:1px solid rgba(245,158,11,.22)}
.iv{font-family:'JetBrains Mono',monospace;font-weight:600;font-size:11px;text-align:right}
.v-ok{color:var(--buy)}.v-no{color:var(--sell)}.v-na{color:var(--dim)}.v-warn{color:var(--hold)}
.cond{font-weight:800;text-align:center;font-size:12px}
.cond-3{color:var(--buy)}.cond-2{color:var(--hold)}.cond-1{color:var(--sell)}.cond-0{color:var(--dim)}

/* === DETAIL BODY === */
.detail-body{padding:18px 16px;border-top:1px solid var(--border)}
.cond-line{font-size:13px;display:flex;gap:10px;margin-bottom:6px;font-family:'JetBrains Mono',monospace;align-items:center}
.cond-label{min-width:75px;font-weight:700;font-size:12px}
.bt-line{margin-top:10px;padding:10px 14px;background:var(--glass);border-radius:var(--radius);border:1px solid var(--glass-border);font-size:12px;font-family:'JetBrains Mono',monospace;color:var(--muted)}
.bt-line b{color:#a5b4fc;font-weight:700}

/* === PERIOD SELECTOR === */
.period-bar{display:flex;gap:2px;margin:10px 0;border-radius:var(--radius);overflow:hidden;border:1px solid var(--border);width:fit-content;padding:3px;background:var(--surface)}
.period-btn{padding:6px 16px;font-size:11px;font-weight:700;font-family:inherit;cursor:pointer;background:transparent;color:var(--muted);border:none;transition:all .15s;letter-spacing:.3px;border-radius:7px}
.period-btn:hover{background:rgba(255,255,255,.06);color:var(--text)}
.period-btn.active{background:var(--accent);color:#fff;box-shadow:0 2px 8px rgba(99,102,241,.3)}

/* === CHARTS === */
.candle-box{width:100%;height:340px;margin:8px 0;border:1px solid var(--border);border-radius:var(--radius);overflow:hidden;background:#060910}
.charts-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-top:14px}
.chart-box{background:var(--bg);border:1px solid var(--border);border-radius:var(--radius);padding:12px}
.chart-box h4{font-size:10px;color:var(--muted);text-transform:uppercase;margin-bottom:8px;letter-spacing:.8px;font-weight:700}
.chart-box canvas{width:100%!important;height:150px!important}

/* === MA LEGEND (inside detail) === */
.ma-legend{display:flex;gap:14px;flex-wrap:wrap;margin:8px 0;font-size:12px;font-family:'JetBrains Mono',monospace}
.ma-legend span{display:flex;align-items:center;gap:5px}
.ma-legend .dot{width:8px;height:8px;border-radius:50%;display:inline-block}

/* === FOOTER === */
.footer{padding:12px 32px;background:var(--surface);border-top:1px solid var(--border);color:var(--dim);font-size:12px;display:flex;justify-content:space-between;font-weight:500}

/* === TOP 3 RECOMMENDATIONS — ACCORDION === */
.top3-section{padding:18px 32px;background:var(--bg);border-bottom:1px solid var(--border)}
.top3-title{font-size:13px;font-weight:800;color:#a5b4fc;text-transform:uppercase;letter-spacing:.8px;margin-bottom:12px;display:flex;align-items:center;gap:8px}
.top3-title::before{content:'';display:inline-block;width:3px;height:16px;background:var(--accent);border-radius:2px}
.top3-empty{color:var(--dim);font-size:12px;font-style:italic;padding:8px 0}
.rec-details{background:var(--card);border:1px solid var(--border);border-radius:var(--radius-lg);margin-bottom:12px;overflow:hidden;transition:all .3s ease}
.rec-details[open]{border-color:rgba(99,102,241,.25);box-shadow:0 4px 24px rgba(99,102,241,.08)}
.rec-details.rec-buy{border-left:3px solid var(--buy)}
.rec-details.rec-sell{border-left:3px solid var(--sell)}
.rec-details.rec-hold{border-left:3px solid var(--hold)}
.rec-details summary{cursor:pointer;padding:16px 20px;display:flex;align-items:center;gap:14px;list-style:none;user-select:none;transition:background .2s}
.rec-details summary:hover{background:rgba(255,255,255,.025)}
.rec-details summary::-webkit-details-marker{display:none}
.rec-details summary::marker{display:none;content:''}
.rec-arrow{font-size:11px;color:var(--muted);transition:transform .2s;flex-shrink:0;width:16px;text-align:center}
.rec-details[open] .rec-arrow{transform:rotate(90deg)}
.rec-rank-badge{display:inline-flex;align-items:center;justify-content:center;width:30px;height:30px;border-radius:8px;font-weight:900;font-size:14px;background:rgba(99,102,241,.12);color:var(--accent);flex-shrink:0}
.rec-sym{font-size:17px;font-weight:900;color:#fff;letter-spacing:-.5px}
.rec-price{font-family:'JetBrains Mono',monospace;font-size:14px;font-weight:600;color:#cbd5e1}
.rec-badge{display:inline-block;padding:3px 12px;border-radius:6px;font-weight:800;font-size:10px;text-transform:uppercase;letter-spacing:.5px}
.rb-buy{background:rgba(16,185,129,.12);color:var(--buy);border:1px solid rgba(16,185,129,.25)}
.rb-sell{background:rgba(239,68,68,.12);color:var(--sell);border:1px solid rgba(239,68,68,.25)}
.rb-hold{background:rgba(245,158,11,.1);color:var(--hold);border:1px solid rgba(245,158,11,.22)}
.rb-buy-near{background:rgba(16,185,129,.08);color:#6ee7b7;border:1px solid rgba(16,185,129,.18)}
.rb-sell-near{background:rgba(239,68,68,.08);color:#fca5a5;border:1px solid rgba(239,68,68,.18)}
.rec-sum-metrics{display:flex;gap:16px;margin-left:auto;font-size:12px;flex-shrink:0;flex-wrap:wrap}
.rec-sm{display:flex;gap:5px;align-items:center}
.rec-sm .lab{color:var(--muted);font-weight:600}
.rec-sm .val{font-family:'JetBrains Mono',monospace;font-weight:700}
.rec-body{padding:0 22px 22px}
.rec-top-row{display:grid;grid-template-columns:1fr 280px;gap:16px;margin-bottom:16px}
.rec-candle-wrap{border-radius:var(--radius);overflow:hidden;border:1px solid var(--border);background:#060910}
.rec-candle-box{width:100%;height:380px}
.rec-candle-legend{display:flex;gap:12px;padding:6px 10px;background:rgba(6,9,16,.6);font-size:10px;flex-wrap:wrap}
.rec-candle-legend span{display:flex;align-items:center;gap:4px;color:var(--muted)}
.rec-candle-legend i{display:inline-block;width:18px;height:2px;border-radius:1px}
.rec-right-panel{display:flex;flex-direction:column;gap:12px}
.rec-metrics{display:grid;grid-template-columns:1fr 1fr;gap:8px 16px}
.rec-m{font-size:13px;display:flex;justify-content:space-between}
.rec-ml{color:var(--muted);font-weight:600}
.rec-mv{font-family:'JetBrains Mono',monospace;font-weight:700}
.rec-levels{background:var(--glass);border:1px solid var(--glass-border);border-radius:var(--radius);padding:14px}
.rec-lt{font-size:10px;font-weight:700;color:#a5b4fc;text-transform:uppercase;letter-spacing:.6px;margin-bottom:8px}
.rec-lr{display:flex;justify-content:space-between;align-items:center;font-size:13px;padding:4px 0}
.rec-ll{color:var(--muted);font-weight:600}
.rec-lv{font-family:'JetBrains Mono',monospace;font-weight:700}
.lv-entry{color:#93c5fd}.lv-target{color:var(--buy)}.lv-stop{color:var(--sell)}.lv-rr{color:var(--accent)}
.rec-thesis{background:linear-gradient(135deg,rgba(99,102,241,.06),rgba(99,102,241,.02));border:1px solid rgba(99,102,241,.15);border-radius:var(--radius);padding:18px 20px;margin-bottom:16px;line-height:1.6}
.rec-thesis-title{font-size:11px;font-weight:800;color:#a5b4fc;text-transform:uppercase;letter-spacing:.8px;margin-bottom:10px}
.rec-thesis-text{font-size:14px;color:var(--text);font-weight:500}
.rec-thesis-meta{display:flex;gap:10px;margin-top:12px;flex-wrap:wrap}
.rec-thesis-horizon{display:inline-block;padding:4px 12px;border-radius:6px;font-size:11px;font-weight:700;background:rgba(99,102,241,.12);color:var(--accent)}
.rec-thesis-target{display:inline-block;padding:4px 12px;border-radius:6px;font-size:11px;font-weight:700;background:rgba(16,185,129,.12);color:var(--buy)}
.rec-sell .rec-thesis-target{background:rgba(239,68,68,.12);color:var(--sell)}
.rec-research-row{display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px;margin-bottom:16px}
.rec-research-panel{background:var(--glass);border:1px solid var(--glass-border);border-radius:var(--radius);padding:16px;min-height:80px}
.rec-fund{background:var(--glass);border:1px solid var(--glass-border);border-radius:var(--radius);padding:16px}
.rec-fund-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:5px 18px}
.rec-fl{font-size:13px;display:flex;justify-content:space-between;padding:2px 0}
.rec-fll{color:var(--muted);font-weight:600}
.rec-flv{font-family:'JetBrains Mono',monospace;font-weight:700;color:var(--text)}
.rec-period-bar{display:flex;gap:2px;margin-bottom:12px;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:3px;width:fit-content}
.rec-period-btn{padding:6px 16px;font-size:11px;font-weight:700;font-family:inherit;cursor:pointer;background:transparent;color:var(--muted);border:none;border-radius:7px;transition:all .15s;letter-spacing:.3px}
.rec-period-btn:hover{background:rgba(255,255,255,.06);color:var(--text)}
.rec-period-btn.active{background:var(--accent);color:#fff;box-shadow:0 2px 8px rgba(99,102,241,.3)}
.rec-ind-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-bottom:14px}
.rec-ind-wrap{border-radius:var(--radius);overflow:hidden;border:1px solid var(--border);background:var(--card)}
.rec-ind-title{font-size:10px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.6px;padding:6px 10px;background:var(--glass)}
.rec-ind-canvas{width:100%;height:190px;display:block}
.rec-rat{border-top:1px solid var(--border);padding-top:12px;margin-top:6px}
.rec-rt{font-size:10px;font-weight:700;color:#a5b4fc;text-transform:uppercase;letter-spacing:.7px;margin-bottom:8px}
.rec-ri{font-size:13px;color:var(--text);padding:4px 0;line-height:1.5}
.rec-ri::before{content:'';display:inline-block;width:4px;height:4px;border-radius:50%;background:var(--accent);margin-right:8px;vertical-align:middle}
.rec-sb{height:4px;border-radius:2px;background:var(--border);margin-top:14px;overflow:hidden}
.rec-sf{height:100%;border-radius:2px;background:linear-gradient(90deg,var(--accent),#818cf8)}

.rec-analyst{margin-bottom:8px}
.rec-analyst-bar{height:8px;border-radius:4px;background:var(--border);position:relative;margin:10px 0 6px}
.rec-analyst-fill{position:absolute;top:0;height:100%;border-radius:4px;background:linear-gradient(90deg,var(--sell),var(--hold),var(--buy))}
.rec-analyst-marker{position:absolute;top:-5px;width:4px;height:18px;border-radius:2px;background:#fff;box-shadow:0 0 6px #fff8}
.rec-analyst-labels{display:flex;justify-content:space-between;font-size:10px;color:var(--muted);font-family:'JetBrains Mono',monospace}
.rec-upside{font-size:16px;font-weight:900;text-align:center;margin:6px 0 2px}
.rec-insider-header{display:flex;align-items:center;gap:8px;margin-bottom:8px}
.rec-sent-badge{display:inline-block;padding:3px 10px;border-radius:6px;font-weight:800;font-size:10px;text-transform:uppercase;letter-spacing:.5px}
.sent-bullish{background:#34d39928;color:var(--buy);border:1px solid #34d39950}
.sent-bearish{background:#f8717128;color:var(--sell);border:1px solid #f8717150}
.sent-neutral{background:#fbbf2422;color:var(--hold);border:1px solid #fbbf2440}
.rec-ins-summary{display:flex;gap:14px;font-size:12px;margin-bottom:6px;font-weight:600}
.rec-ins-tx{font-size:11px;color:#b8c5d6;padding:3px 0;border-top:1px solid #ffffff14;line-height:1.4}
.rec-earnings{display:flex;align-items:center;gap:12px}
.rec-earn-badge{display:inline-flex;align-items:center;justify-content:center;min-width:42px;height:42px;border-radius:10px;font-weight:900;font-size:15px;background:#818cf828;color:var(--accent)}
.rec-earn-info{font-size:12px}
.rec-earn-warn{color:var(--sell);font-weight:700}
.rec-ratio-divider{border:none;border-top:1px solid var(--border);margin:8px 0}
.list-header span[data-col]{cursor:pointer;user-select:none;transition:color .15s}
.list-header span[data-col]:hover{color:var(--text)}
.sort-arrow{font-size:8px;margin-left:2px;opacity:.7}

/* === NAV TABS === */
.nav-tabs{display:flex;gap:0;padding:0 32px;background:var(--surface);border-bottom:1px solid var(--border)}
.nav-tab{padding:12px 24px;font-size:13px;font-weight:700;color:var(--muted);cursor:pointer;border:none;background:none;border-bottom:2px solid transparent;margin-bottom:-1px;transition:all .2s;letter-spacing:.3px;position:relative}
.nav-tab:hover{color:var(--text);background:rgba(255,255,255,.02)}
.nav-tab.active{color:var(--accent);border-bottom-color:var(--accent)}
.tab-content{display:none}.tab-content.active{display:block}

/* === PORTFOLIO SECTION === */
.portfolio-section{padding:18px 32px}
.port-title{font-size:17px;font-weight:800;color:#fff;margin-bottom:16px;letter-spacing:-.3px}
.port-title em{font-style:normal;color:var(--accent)}

.port-summary{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-bottom:20px}
.port-card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius-lg);padding:16px 18px;transition:border-color .2s}
.port-card:hover{border-color:rgba(99,102,241,.2)}
.port-card-label{font-size:10px;text-transform:uppercase;letter-spacing:.7px;color:var(--muted);margin-bottom:6px;font-weight:600}
.port-card-value{font-size:22px;font-weight:800;letter-spacing:-.5px}
.port-card-sub{font-size:12px;color:var(--muted);margin-top:3px}

.port-alerts{margin-bottom:24px;display:flex;flex-direction:column;gap:10px}
.port-alerts:empty{display:none}
.port-alert{display:flex;align-items:center;gap:16px;padding:16px 20px;border-radius:var(--radius-lg);font-size:13px;border-left:4px solid;background:var(--card)}
.port-alert-cta{display:flex;flex-direction:column;flex:1;gap:3px}
.port-alert-head{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.port-alert-action-badge{font-size:11px;font-weight:800;padding:4px 12px;border-radius:6px;letter-spacing:.6px;text-transform:uppercase}
.port-alert-symbol{font-size:17px;font-weight:800;color:var(--text);letter-spacing:-.3px}
.port-alert-price{font-size:13px;color:var(--muted);font-variant-numeric:tabular-nums}
.port-alert-reason{font-size:13px;color:var(--muted);line-height:1.6}
.port-alert-danger{background:rgba(239,68,68,.04);border-color:#ef4444}
.port-alert-danger .port-alert-action-badge{background:#ef4444;color:#fff}
.port-alert-warning{background:rgba(245,158,11,.04);border-color:#f59e0b}
.port-alert-warning .port-alert-action-badge{background:#f59e0b;color:#1a1a1a}
.port-alert-success{background:rgba(16,185,129,.04);border-color:#10b981}
.port-alert-success .port-alert-action-badge{background:#10b981;color:#0b1120}
.port-alert-info{background:rgba(99,102,241,.04);border-color:#6366f1}
.port-alert-info .port-alert-action-badge{background:#6366f1;color:#fff}
.port-alert-jump{background:transparent;border:1px solid var(--border);color:var(--text);font-size:11px;font-weight:700;padding:7px 14px;border-radius:8px;cursor:pointer;letter-spacing:.4px;text-transform:uppercase;transition:all .2s}
.port-alert-jump:hover{background:var(--accent);border-color:var(--accent);color:#fff}
.port-alerts-empty{color:var(--muted);font-size:13px;padding:14px 18px;background:var(--card);border:1px dashed var(--border);border-radius:var(--radius);text-align:center}

.port-verdicts{margin-bottom:28px}
.port-verdicts-title{font-size:14px;font-weight:700;color:var(--text);margin-bottom:12px;letter-spacing:-.2px}
.port-verdicts-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px}
.port-verdict-card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius-lg);padding:14px 16px;cursor:pointer;transition:all .2s ease;border-left:4px solid var(--border);position:relative}
.port-verdict-card:hover{transform:translateY(-2px);border-color:rgba(99,102,241,.3);box-shadow:var(--shadow-md)}
.port-verdict-card.v-sell{border-left-color:#ef4444}
.port-verdict-card.v-add{border-left-color:#10b981}
.port-verdict-card.v-hold{border-left-color:#6366f1}
.port-verdict-card.v-reduce{border-left-color:#f59e0b}
.port-verdict-head{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px}
.port-verdict-sym{font-size:17px;font-weight:800;color:var(--text);letter-spacing:-.3px}
.port-verdict-sub{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.4px;margin-top:3px}
.port-verdict-action{font-size:11px;font-weight:800;padding:4px 10px;border-radius:6px;letter-spacing:.5px}
.port-verdict-action.v-sell{background:rgba(239,68,68,.12);color:#fca5a5}
.port-verdict-action.v-add{background:rgba(16,185,129,.12);color:#34d399}
.port-verdict-action.v-hold{background:rgba(99,102,241,.12);color:#a5b4fc}
.port-verdict-action.v-reduce{background:rgba(245,158,11,.12);color:#fcd34d}
.port-verdict-metrics{display:flex;gap:16px;font-size:12px;color:var(--muted);flex-wrap:wrap;margin-bottom:8px}
.port-verdict-metrics b{color:var(--text);font-variant-numeric:tabular-nums;font-weight:700}
.port-verdict-trend{display:flex;align-items:center;gap:5px;font-size:11px;text-transform:uppercase;letter-spacing:.5px;font-weight:700}
.port-verdict-trend.t-up{color:#10b981}
.port-verdict-trend.t-down{color:#ef4444}
.port-verdict-trend.t-flat{color:var(--muted)}
.port-verdict-reason{font-size:12px;color:var(--muted);line-height:1.5}
.port-verdict-indi{display:flex;gap:6px;margin-top:10px}
.port-verdict-indi-chip{font-size:9px;padding:3px 8px;border-radius:5px;font-weight:700;letter-spacing:.4px}
.port-verdict-indi-chip.ok{background:rgba(16,185,129,.1);color:#34d399}
.port-verdict-indi-chip.no{background:rgba(239,68,68,.1);color:#f87171}

.port-analysis{margin-top:12px}
.port-analysis-title{font-size:14px;font-weight:700;color:var(--text);margin-bottom:12px;letter-spacing:-.2px}
.port-analysis-list-empty{font-size:13px;color:var(--muted);padding:24px;text-align:center;background:var(--card);border:1px dashed var(--border);border-radius:var(--radius)}

.port-table{width:100%;border-collapse:collapse;font-size:13px}
.port-table th{text-align:left;padding:10px 12px;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.6px;border-bottom:1px solid var(--border);background:var(--surface);position:sticky;top:0;font-weight:700}
.port-table td{padding:9px 12px;border-bottom:1px solid rgba(30,39,54,.5)}
.port-table tr:hover{background:rgba(255,255,255,.02)}

/* === SETUP TAB === */
.setup-section{padding:18px 32px}
.setup-card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius-lg);padding:28px 32px;max-width:650px;margin:0 auto;text-align:center}
.setup-card h2{font-size:22px;font-weight:800;color:#fff;margin-bottom:8px}
.setup-card .sub{color:var(--muted);font-size:14px;margin-bottom:28px}
.setup-steps{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:20px;text-align:left;margin-bottom:20px}
.setup-step{display:flex;align-items:flex-start;gap:12px;margin-bottom:20px}
.setup-step:last-child{margin-bottom:0}
.setup-num{background:var(--accent);color:#fff;width:28px;height:28px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-weight:700;flex-shrink:0;font-size:14px}
.setup-step-content{flex:1}
.setup-step-content h3{font-size:15px;font-weight:600;color:#fff;margin-bottom:4px}
.setup-step-content p{font-size:13px;color:var(--muted);line-height:1.5}
.setup-step-content code{background:var(--bg);padding:2px 6px;border-radius:4px;font-size:12px;color:#f0883e}
.setup-pre{background:var(--bg);padding:12px;border-radius:6px;font-size:12px;color:#c9d1d9;overflow-x:auto;margin:8px 0;position:relative;font-family:'JetBrains Mono',monospace}
.setup-copy-btn{position:absolute;top:6px;right:6px;padding:4px 12px;background:var(--accent);color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:12px;font-weight:600}
.setup-status-card{background:var(--bg);border:1px solid var(--border);border-radius:var(--radius);padding:16px;text-align:left;margin-bottom:20px}
.setup-token-box{background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:10px 14px;font-family:'JetBrains Mono',monospace;font-size:11px;color:#f0883e;word-break:break-all;margin:8px 0}

/* === OPTIONS LAB === */
.olab-summary{padding:18px 22px;background:linear-gradient(135deg,rgba(99,102,241,.06),rgba(139,92,246,.06));border:1px solid rgba(99,102,241,.15);border-radius:var(--radius-lg);margin:14px 22px;display:flex;gap:18px;align-items:center;flex-wrap:wrap}
.olab-summary-icon{font-size:28px;width:52px;height:52px;display:flex;align-items:center;justify-content:center;border-radius:var(--radius);background:rgba(99,102,241,.1)}
.olab-summary-text{flex:1;min-width:200px}
.olab-summary-text h3{margin:0 0 6px;font-size:16px;color:var(--text)}
.olab-summary-text p{margin:0;font-size:13px;color:var(--muted);line-height:1.6}
.olab-section{margin:14px 22px}
.olab-section-title{font-size:15px;font-weight:800;color:var(--text);margin-bottom:14px;letter-spacing:.3px;display:flex;align-items:center;gap:10px}
.olab-section-title .olab-badge{font-size:10px;padding:3px 10px;border-radius:5px;font-weight:700}
.olab-iv-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
.olab-iv-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:14px 16px;transition:border-color .2s}
.olab-iv-card:hover{border-color:rgba(99,102,241,.2)}
.olab-iv-card .label{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px;margin-bottom:5px;font-weight:600}
.olab-iv-card .value{font-size:22px;font-weight:800;color:var(--text);font-variant-numeric:tabular-nums}
.olab-iv-card .sub{font-size:11px;color:var(--muted);margin-top:3px}
.olab-iv-alert{background:rgba(245,158,11,.08);border:1px solid rgba(245,158,11,.25);border-radius:8px;padding:12px 16px;margin-bottom:10px;font-size:12px;color:var(--text);line-height:1.5}
.olab-iv-alert strong{color:#f59e0b}
.olab-strat-list{display:flex;flex-direction:column;gap:12px}
.olab-strat-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-lg);overflow:hidden;transition:all .2s ease}
.olab-strat-card:hover{border-color:rgba(99,102,241,.25)}
.olab-strat-header{padding:16px 18px;display:flex;align-items:center;gap:14px;cursor:pointer}
.olab-strat-rank{width:34px;height:34px;border-radius:8px;display:flex;align-items:center;justify-content:center;font-weight:900;font-size:15px;flex-shrink:0}
.olab-strat-rank.gold{background:linear-gradient(135deg,#f59e0b,#d97706);color:#000}
.olab-strat-rank.silver{background:linear-gradient(135deg,#94a3b8,#64748b);color:#fff}
.olab-strat-rank.bronze{background:linear-gradient(135deg,#d97706,#92400e);color:#fff}
.olab-strat-rank.normal{background:var(--border);color:var(--text)}
.olab-strat-info{flex:1;min-width:0}
.olab-strat-name{font-size:15px;font-weight:800;color:var(--text)}
.olab-strat-desc{font-size:12px;color:var(--muted);margin-top:4px;line-height:1.5}
.olab-strat-metrics{display:flex;gap:18px;flex-wrap:wrap;align-items:center}
.olab-strat-metric{text-align:center}
.olab-strat-metric .val{font-size:17px;font-weight:800;font-variant-numeric:tabular-nums}
.olab-strat-metric .lbl{font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:.4px}
.olab-strat-score{width:50px;height:50px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-weight:900;font-size:16px;flex-shrink:0}
.olab-strat-expand{font-size:18px;color:var(--muted);flex-shrink:0;transition:transform .2s}
.olab-strat-card.open .olab-strat-expand{transform:rotate(180deg)}
.olab-strat-body{display:none;padding:0 18px 18px;border-top:1px solid var(--border)}
.olab-strat-card.open .olab-strat-body{display:block}
.olab-detail-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:14px}
@media(max-width:900px){.olab-detail-grid{grid-template-columns:1fr}}
.olab-payoff-chart{background:var(--bg);border-radius:8px;border:1px solid var(--border);padding:12px;min-height:200px}
.olab-payoff-canvas{width:100%;height:200px}
.olab-greeks-table{width:100%;border-collapse:collapse;font-size:12px}
.olab-greeks-table th{text-align:left;padding:6px 10px;color:var(--muted);font-size:10px;text-transform:uppercase;border-bottom:1px solid var(--border)}
.olab-greeks-table td{padding:6px 10px;border-bottom:1px solid var(--border)22;font-variant-numeric:tabular-nums}
.olab-legs-table{width:100%;border-collapse:collapse;font-size:11px;margin-top:10px}
.olab-legs-table th{text-align:left;padding:5px 8px;color:var(--muted);font-size:9px;text-transform:uppercase;border-bottom:1px solid var(--border)}
.olab-legs-table td{padding:5px 8px;border-bottom:1px solid var(--border)22}
.olab-bt-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.olab-bt-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:12px 14px;text-align:center;transition:border-color .2s}
.olab-bt-card:hover{border-color:rgba(99,102,241,.2)}
.olab-bt-card .days{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px;margin-bottom:7px;font-weight:600}
.olab-bt-card .ret{font-size:20px;font-weight:800;font-variant-numeric:tabular-nums}
.olab-bt-card .wr{font-size:12px;margin-top:3px}
.olab-bt-card .range{font-size:11px;color:var(--muted);margin-top:5px}
.olab-bt-hist{margin-top:10px;height:44px;display:flex;align-items:flex-end;gap:1px}
.olab-bt-bar{flex:1;border-radius:1px 1px 0 0;min-width:2px}
.olab-bt-context{font-size:13px;color:var(--muted);line-height:1.6;padding:12px 0}
.olab-bias{display:inline-block;padding:2px 8px;border-radius:4px;font-size:10px;font-weight:700;letter-spacing:.3px}
.olab-bias.bullish{background:rgba(16,185,129,.15);color:#10b981;border:1px solid rgba(16,185,129,.3)}
.olab-bias.bearish{background:rgba(239,68,68,.15);color:#ef4444;border:1px solid rgba(239,68,68,.3)}
.olab-bias.neutral{background:rgba(99,102,241,.15);color:#818cf8;border:1px solid rgba(99,102,241,.3)}
.olab-multi-card{background:var(--surface);border:1px solid var(--border);border-radius:10px;margin-bottom:16px;overflow:hidden}
.olab-multi-header{padding:14px 16px;display:flex;align-items:center;gap:12px;cursor:pointer;border-bottom:1px solid var(--border)}
.olab-multi-header h3{margin:0;font-size:15px;font-weight:800;color:var(--text)}
.olab-multi-header .signal-tag{margin-left:auto}
.olab-multi-body{padding:16px;display:none}
.olab-multi-card.open .olab-multi-body{display:block}

/* === TRADES HISTORY === */
.th-section{padding:18px 32px}
.th-title{font-size:17px;font-weight:800;color:#fff;margin-bottom:16px;letter-spacing:-.3px}
.th-title em{font-style:normal;color:var(--accent)}
.th-summary{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:20px}
.th-card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius-lg);padding:14px 16px}
.th-card .label{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px;margin-bottom:4px;font-weight:600}
.th-card .value{font-size:22px;font-weight:800;color:var(--text);font-variant-numeric:tabular-nums}
.th-card .sub{font-size:11px;color:var(--muted);margin-top:3px}
.th-filters{display:flex;gap:8px;margin-bottom:18px;flex-wrap:wrap}
.th-filter-btn{padding:7px 16px;font-size:11px;font-weight:700;color:var(--muted);cursor:pointer;border:1px solid var(--border);background:var(--surface);border-radius:6px;transition:all .2s;letter-spacing:.3px;text-transform:uppercase}
.th-filter-btn:hover{color:var(--text);border-color:var(--accent)44}
.th-filter-btn.active{color:var(--accent);border-color:var(--accent);background:rgba(99,102,241,.08)}
.th-list{display:flex;flex-direction:column;gap:10px}
.th-trade{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-lg);overflow:hidden;transition:all .2s}
.th-trade:hover{border-color:rgba(99,102,241,.2)}
.th-trade-header{padding:14px 18px;display:flex;align-items:center;gap:14px;cursor:pointer}
.th-trade-sym{font-size:16px;font-weight:900;color:var(--text);min-width:60px}
.th-trade-badge{padding:3px 10px;border-radius:5px;font-size:10px;font-weight:700;letter-spacing:.3px}
.th-trade-badge.win{background:rgba(16,185,129,.15);color:#10b981;border:1px solid rgba(16,185,129,.3)}
.th-trade-badge.loss{background:rgba(239,68,68,.15);color:#ef4444;border:1px solid rgba(239,68,68,.3)}
.th-trade-badge.stk{background:rgba(99,102,241,.12);color:var(--accent);border:1px solid rgba(99,102,241,.25)}
.th-trade-badge.opt{background:rgba(251,191,36,.12);color:#fbbf24;border:1px solid rgba(251,191,36,.25)}
.th-trade-badge.spread{background:rgba(192,132,252,.12);color:#c084fc;border:1px solid rgba(192,132,252,.25)}
.th-trade-badge.estimated{background:rgba(251,191,36,.12);color:#fbbf24;border:1px solid rgba(251,191,36,.25);font-size:9px}
.th-trade-dates{font-size:12px;color:var(--muted);flex:1;min-width:0}
.th-trade-metrics{display:flex;gap:18px;align-items:center}
.th-trade-metric{text-align:right}
.th-trade-metric .val{font-size:15px;font-weight:800;font-variant-numeric:tabular-nums}
.th-trade-metric .lbl{font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:.4px}
.th-trade-expand{font-size:18px;color:var(--muted);flex-shrink:0;transition:transform .2s}
.th-trade.open .th-trade-expand{transform:rotate(180deg)}
.th-trade-body{display:none;padding:0 18px 18px;border-top:1px solid var(--border)}
.th-trade.open .th-trade-body{display:block}
.th-detail-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:14px}
@media(max-width:900px){.th-detail-grid{grid-template-columns:1fr}}
.th-detail-section{margin-bottom:14px}
.th-detail-title{font-size:12px;font-weight:800;color:var(--accent);text-transform:uppercase;letter-spacing:.5px;margin-bottom:8px;display:flex;align-items:center;gap:6px}
.th-detail-text{font-size:13px;color:#b8c5d6;line-height:1.7}
.th-fills-table{width:100%;border-collapse:collapse;font-size:11px;margin-top:6px}
.th-fills-table th{text-align:left;padding:5px 8px;color:var(--muted);font-size:9px;text-transform:uppercase;border-bottom:1px solid var(--border)}
.th-fills-table td{padding:5px 8px;border-bottom:1px solid var(--border)22;font-variant-numeric:tabular-nums}
.th-chart-container{background:var(--bg);border-radius:8px;border:1px solid var(--border);min-height:300px;margin-bottom:12px}
.th-chart-row{display:grid;grid-template-columns:1fr;gap:8px}
.th-ind-charts{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px}
.th-ind-chart{background:var(--bg);border-radius:6px;border:1px solid var(--border);padding:6px;min-height:140px}
.th-ind-chart canvas{width:100%!important;height:130px!important}
.th-context-box{background:rgba(99,102,241,.04);border:1px solid rgba(99,102,241,.12);border-radius:8px;padding:12px 16px;margin-top:8px}
.th-context-box .th-ctx-label{font-size:10px;color:var(--accent);text-transform:uppercase;letter-spacing:.5px;font-weight:700;margin-bottom:4px}
.th-context-box .th-ctx-text{font-size:12px;color:var(--muted);line-height:1.6}
.th-lessons-box{background:rgba(251,191,36,.04);border:1px solid rgba(251,191,36,.12);border-radius:8px;padding:12px 16px;margin-top:8px}
.th-lessons-box .th-les-label{font-size:10px;color:#fbbf24;text-transform:uppercase;letter-spacing:.5px;font-weight:700;margin-bottom:4px}
.th-lessons-box .th-les-text{font-size:12px;color:var(--muted);line-height:1.6}
.th-trade-pnl-bar{height:4px;border-radius:2px;margin-top:6px;background:var(--border)}
.th-trade-pnl-fill{height:100%;border-radius:2px}

/* Technical observations */
.obs-row{display:flex;gap:8px;padding:3px 14px 5px 32px;flex-wrap:wrap;align-items:center}
.obs-tag{display:inline-flex;align-items:center;gap:4px;padding:3px 10px;border-radius:5px;font-size:9px;font-weight:700;letter-spacing:.4px;border:1px solid;flex-shrink:0}
.obs-text{font-size:11px;color:var(--muted);line-height:1.4}
.obs-spark{width:60px;height:18px;vertical-align:middle;border-radius:3px}

/* === RESPONSIVE === */
@media(max-width:1100px){.charts-grid{grid-template-columns:1fr}.content{padding:0 12px 12px}.rec-top-row{grid-template-columns:1fr}.rec-ind-grid{grid-template-columns:1fr}.rec-research-row{grid-template-columns:1fr}.rec-fund-grid{grid-template-columns:1fr 1fr}}
@media(max-width:700px){.header{flex-direction:column;gap:4px}.header .sub{text-align:left}.top3-section{padding:12px}.rec-sum-metrics{display:none}.portfolio-section{padding:12px}.nav-tabs{padding:0 12px}.nav-tab{padding:8px 14px;font-size:12px}.th-section{padding:12px}.th-summary{grid-template-columns:1fr 1fr}.th-trade-metrics{gap:10px}.th-ind-charts{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="header">
  <h1><em>IB TRADING</em> DASHBOARD</h1>
  <div class="header-right">
    <div class="bridge-status">
      <span class="bridge-dot off" id="bridge-dot"></span>
      <span id="bridge-status-text">Verificando...</span>
    </div>
    <span class="user-email" id="user-email"></span>
    <a href="/logout" class="btn-logout">Salir</a>
  </div>
</div>
<div class="counters" id="counters"></div>
<div class="nav-tabs">
  <button class="nav-tab active" onclick="switchTab('scanner')">Scanner</button>
  <button class="nav-tab" onclick="switchTab('portfolio')">Mi Cartera</button>
  <button class="nav-tab" onclick="switchTab('optionslab')">Options Lab</button>
  <button class="nav-tab" onclick="switchTab('trades')">Trades Historicos</button>
  <button class="nav-tab" onclick="switchTab('setup')">Conectar TWS</button>
</div>

<!-- TAB: SCANNER -->
<div id="tab-scanner" class="tab-content active">
<div id="top3-section" class="top3-section" style="display:none"></div>
<div class="content">
  <div class="list-header" id="list-header">
    <span></span><span data-col="sym" onclick="sortListBy('sym')">Ticker</span><span data-col="price" style="text-align:right" onclick="sortListBy('price')">Precio</span>
    <span data-col="signal" onclick="sortListBy('signal')">Senal</span><span data-col="strength" style="text-align:right" title="Fuerza de la senal (0-5.1)" onclick="sortListBy('strength')">Str</span>
    <span class="sep" data-col="macd" style="text-align:right" onclick="sortListBy('macd')">MACD</span><span data-col="rsi" style="text-align:right" onclick="sortListBy('rsi')">RSI</span>
    <span data-col="konc" style="text-align:right" onclick="sortListBy('konc')">Konc</span><span data-col="cond" onclick="sortListBy('cond')">C</span>
    <span></span><span></span><span></span><span></span><span></span><span></span><span></span><span></span>
  </div>
  <div id="stock-list"></div>
</div>
</div>

<!-- TAB: MI CARTERA -->
<div id="tab-portfolio" class="tab-content">
<div class="portfolio-section" id="portfolio-section">
  <div class="port-title"><em>MI CARTERA</em> &mdash; Posiciones & Analisis</div>
  <div id="port-loading" style="color:var(--muted);text-align:center;padding:40px">Cargando cartera...</div>
  <div id="port-content" style="display:none">
    <div class="port-summary" id="port-summary"></div>
    <div class="port-alerts" id="port-alerts"></div>
    <div class="port-verdicts">
      <div class="port-verdicts-title">Que hacer con cada posicion</div>
      <div class="port-verdicts-grid" id="port-verdicts"></div>
    </div>
    <div class="port-analysis">
      <div class="port-analysis-title">Analisis detallado por posicion</div>
      <div id="port-analysis-list"></div>
    </div>
  </div>
</div>
</div>

<!-- TAB: OPTIONS LAB -->
<div id="tab-optionslab" class="tab-content">
<div class="portfolio-section" id="optionslab-section">
  <div class="port-title"><em>OPTIONS LAB</em> &mdash; Estrategias de Opciones</div>
  <div id="olab-loading" style="color:var(--muted);text-align:center;padding:40px">
    <p style="font-size:16px;margin-bottom:8px">Options Lab</p>
    <p style="color:var(--dim)">Proximamente disponible en la version cloud.</p>
  </div>
  <div id="olab-content" style="display:none"></div>
  <div id="olab-multi" style="display:none"></div>
</div>
</div>

<!-- TAB: TRADES HISTORICOS -->
<div id="tab-trades" class="tab-content">
<div class="th-section" id="trades-section">
  <div class="th-title"><em>TRADES HISTORICOS</em> &mdash; Analisis de Operaciones Cerradas</div>
  <div id="th-loading" style="color:var(--muted);text-align:center;padding:40px">
    <p style="font-size:16px;margin-bottom:8px">Trades Historicos</p>
    <p style="color:var(--dim)">Proximamente disponible en la version cloud.</p>
  </div>
  <div id="th-content" style="display:none"></div>
</div>
</div>

<!-- TAB: CONECTAR TWS -->
<div id="tab-setup" class="tab-content">
<div class="setup-section">
  <div class="setup-card">
    <h2>Conectar tu TWS</h2>
    <p class="sub">Solo necesitas TWS abierta y seguir estos 3 pasos.</p>

    <div class="setup-steps">
      <div class="setup-step">
        <span class="setup-num">1</span>
        <div class="setup-step-content">
          <h3>Abri TWS</h3>
          <p>Abre Trader Workstation y habilita la API:<br>
          <code>Edit → Global Configuration → API → Settings</code><br>
          ✓ Enable ActiveX and Socket Clients &nbsp; ✓ Puerto: <code>7497</code></p>
        </div>
      </div>

      <div class="setup-step">
        <span class="setup-num">2</span>
        <div class="setup-step-content">
          <h3>Instalar el Bridge <span style="font-size:11px;color:var(--muted);font-weight:400">(solo la primera vez)</span></h3>
          <p style="margin-bottom:8px">Abri la Terminal y pega este comando:</p>
          <div style="position:relative">
            <div class="setup-pre" id="install-cmd"></div>
            <button class="setup-copy-btn" id="install-btn" onclick="copyCmd('install-cmd','install-btn')">Copiar</button>
          </div>
          <p style="font-size:11px;color:var(--muted);margin-top:6px">Requiere Python 3.10+ &nbsp;|&nbsp; Se instala en <code>~/.ib-bridge/</code></p>
        </div>
      </div>

      <div class="setup-step">
        <span class="setup-num">3</span>
        <div class="setup-step-content">
          <h3>Conectar</h3>
          <p style="margin-bottom:8px">Cada vez que quieras conectar, pega esto en la Terminal:</p>
          <div style="position:relative">
            <div class="setup-pre" id="run-cmd"></div>
            <button class="setup-copy-btn" id="run-btn" onclick="copyCmd('run-cmd','run-btn')">Copiar</button>
          </div>
          <p style="font-size:11px;color:var(--muted);margin-top:6px">El indicador arriba cambiara a <span style="color:var(--buy)">● Conectado</span></p>
        </div>
      </div>
    </div>

    <div class="setup-status-card">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <div>
          <p style="font-size:12px;color:var(--muted)">Estado de conexion</p>
          <p id="setup-live-status" style="font-size:14px;margin-top:4px">Verificando...</p>
        </div>
        <div id="setup-status-dot" style="width:12px;height:12px;border-radius:50%;background:var(--dim)"></div>
      </div>
    </div>

    <details style="text-align:left">
      <summary style="color:var(--accent);cursor:pointer;font-size:13px">Opciones avanzadas</summary>
      <div style="margin-top:12px;padding:12px;background:var(--bg);border-radius:6px">
        <p style="font-size:12px;color:var(--muted);margin-bottom:4px">Tu bridge token (no lo compartas):</p>
        <div class="setup-token-box" id="token-display">Cargando...</div>
        <button style="background:var(--accent);color:#fff;border:none;padding:6px 14px;border-radius:6px;cursor:pointer;font-size:11px;font-weight:600;margin-top:8px" onclick="regenerateToken()">Regenerar Token</button>
        <p style="font-size:11px;color:var(--muted);margin-top:12px">Puerto 7497 = paper trading &nbsp;|&nbsp; Agrega <code>--ib-port 7496</code> para live</p>
      </div>
    </details>
  </div>
</div>
</div>

<div class="footer">
  <span>Actualizado: <span id="last-update">--</span></span>
  <span>MACD + RSI + KONCORDE</span>
</div>

<script>
const REFRESH_MS=15000;
const DAILY_BARS={'ALL':9999,'5Y':9999,'1Y':252,'3M':63,'1M':22,'1W':5,'1D':1};
let _data=null,_charts={},_periods={},_intradayCache={};
let _activeTab='scanner';
let _portData=null;
let _portLoaded=false;
let _bridgeConnected=false;
let _bridgeToken='';
let _userEmail='';

// ═══════════════════════════════════════════
//  AUTH-AWARE FETCH
// ═══════════════════════════════════════════
async function authFetch(url,opts){
  let r=await fetch(url,opts);
  if(r.status===401){window.location='/login';throw new Error('unauthorized');}
  return r;
}

// ═══════════════════════════════════════════
//  TABS
// ═══════════════════════════════════════════
function switchTab(tab){
  _activeTab=tab;
  document.querySelectorAll('.nav-tab').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(t=>t.classList.remove('active'));
  document.querySelector('.nav-tab[onclick*="\''+tab+'\'"]').classList.add('active');
  document.getElementById('tab-'+tab).classList.add('active');
  if(tab==='portfolio'&&!_portLoaded){_portLoaded=true;loadPortfolio();}
  if(tab==='setup')renderSetup();
}

// ═══════════════════════════════════════════
//  STATUS + BRIDGE TOKEN
// ═══════════════════════════════════════════
async function fetchStatus(){
  try{
    let r=await authFetch('/api/status');
    let d=await r.json();
    _bridgeConnected=d.bridge_connected;
    _userEmail=d.email||'';
    document.getElementById('bridge-dot').className='bridge-dot '+(_bridgeConnected?'on':'off');
    document.getElementById('bridge-status-text').textContent=_bridgeConnected
      ?'Conectado — '+d.stocks_count+' acciones'+(d.last_update?' ('+d.last_update+')':'')
      :'TWS Desconectado';
    if(_userEmail)document.getElementById('user-email').textContent=_userEmail;
  }catch(e){}
}

async function fetchBridgeToken(){
  try{
    let r=await authFetch('/api/bridge-token');
    let d=await r.json();
    _bridgeToken=d.bridge_token||'';
  }catch(e){}
}

async function regenerateToken(){
  if(!confirm('Regenerar token? El bridge actual se desconectara.'))return;
  try{
    let r=await authFetch('/api/bridge-token/regenerate',{method:'POST'});
    let d=await r.json();
    _bridgeToken=d.bridge_token||'';
    renderSetup();
  }catch(e){}
}

function renderSetup(){
  let serverUrl=window.location.origin;
  let installCmd=document.getElementById('install-cmd');
  let runCmd=document.getElementById('run-cmd');
  if(installCmd)installCmd.textContent='curl -sL '+serverUrl+'/install.sh | bash';
  if(runCmd)runCmd.textContent='~/.ib-bridge/run-bridge.sh '+serverUrl+' '+(_bridgeToken||'TOKEN');
  let tokenEl=document.getElementById('token-display');
  if(tokenEl)tokenEl.textContent=_bridgeToken||'Cargando...';
  let statusEl=document.getElementById('setup-live-status');
  let dotEl=document.getElementById('setup-status-dot');
  if(statusEl&&dotEl){
    if(_bridgeConnected){
      statusEl.innerHTML='<span style="color:var(--buy);font-weight:600">Conectado</span> — recibiendo datos de TWS';
      dotEl.style.background='var(--buy)';
    }else{
      statusEl.innerHTML='<span style="color:var(--dim)">Desconectado</span> — segui los pasos de arriba para conectar';
      dotEl.style.background='var(--dim)';
    }
  }
}

function copyCmd(preId,btnId){
  let text=document.getElementById(preId).textContent;
  navigator.clipboard.writeText(text);
  let btn=document.getElementById(btnId);
  btn.textContent='Copiado!';
  setTimeout(()=>{btn.textContent='Copiar'},2000);
}

// ═══════════════════════════════════════════
//  PORTFOLIO
// ═══════════════════════════════════════════
function loadPortfolio(){
  document.getElementById('port-loading').style.display='';
  document.getElementById('port-content').style.display='none';
  authFetch('/api/portfolio').then(r=>r.json()).then(data=>{
    _portData=data;
    if(data.error){
      document.getElementById('port-loading').textContent='Error: '+data.error;
      return;
    }
    document.getElementById('port-loading').style.display='none';
    document.getElementById('port-content').style.display='';
    renderPortfolio(data);
  }).catch(err=>{
    if(err.message==='unauthorized')return;
    document.getElementById('port-loading').textContent='Error cargando cartera: '+err.message;
  });
}

function _portVerdictClass(v){
  if(v==='SELL')return 'v-sell';
  if(v==='ADD'||v==='BUY')return 'v-add';
  if(v==='REDUCE')return 'v-reduce';
  return 'v-hold';
}
function _portVerdictLabel(v){
  if(v==='SELL')return 'VENDER';
  if(v==='ADD')return 'SUMAR';
  if(v==='BUY')return 'COMPRAR';
  if(v==='REDUCE')return 'REDUCIR';
  return 'HOLD';
}

function renderPortfolio(d){
  let positions=d.positions||[];
  let sumHtml='';

  // For cloud, we render a simpler portfolio from bridge data
  if(positions.length===0){
    document.getElementById('port-summary').innerHTML='<div class="port-card"><div class="port-card-label">Posiciones</div><div class="port-card-value" style="color:var(--muted)">0</div><div class="port-card-sub">Conecta TWS para ver tu cartera</div></div>';
    document.getElementById('port-alerts').innerHTML='<div class="port-alerts-empty">Conecta TWS para ver alertas y recomendaciones.</div>';
    document.getElementById('port-verdicts').innerHTML='<div class="port-analysis-list-empty">Sin posiciones abiertas.</div>';
    document.getElementById('port-analysis-list').innerHTML='';
    return;
  }

  let totalValue=0,totalPnl=0;
  positions.forEach(p=>{
    totalValue+=p.marketValue||0;
    totalPnl+=p.unrealizedPNL||0;
  });

  let pnlCol=totalPnl>=0?'var(--buy)':'var(--sell)';
  let pnlSign=totalPnl>=0?'+':'';

  sumHtml+='<div class="port-card"><div class="port-card-label">Valor Total</div><div class="port-card-value" style="color:var(--accent)">$'+fmtN(totalValue)+'</div></div>';
  sumHtml+='<div class="port-card"><div class="port-card-label">P&L No Realizado</div><div class="port-card-value" style="color:'+pnlCol+'">'+pnlSign+'$'+fmtN(Math.abs(totalPnl))+'</div></div>';
  sumHtml+='<div class="port-card"><div class="port-card-label">Posiciones</div><div class="port-card-value" style="color:var(--text)">'+positions.length+'</div></div>';

  let acct=d.account_values||{};
  if(acct.NetLiquidation)sumHtml+='<div class="port-card"><div class="port-card-label">Liquidacion Neta</div><div class="port-card-value" style="color:var(--text)">$'+fmtN(acct.NetLiquidation)+'</div></div>';
  if(acct.TotalCashValue)sumHtml+='<div class="port-card"><div class="port-card-label">Efectivo</div><div class="port-card-value" style="color:var(--text)">$'+fmtN(acct.TotalCashValue)+'</div></div>';
  if(acct.BuyingPower)sumHtml+='<div class="port-card"><div class="port-card-label">Poder de Compra</div><div class="port-card-value" style="color:var(--text)">$'+fmtN(acct.BuyingPower)+'</div></div>';

  document.getElementById('port-summary').innerHTML=sumHtml;
  document.getElementById('port-alerts').innerHTML='<div class="port-alerts-empty">Alertas disponibles con analisis completo (version local).</div>';

  // Render positions as simple table with scanner data overlay
  let vHtml='';
  for(let p of positions){
    let pnl=p.unrealizedPNL||0;
    let pnlPct=p.averageCost>0?((p.marketPrice-p.averageCost)/p.averageCost*100):0;
    let pnlC=pnl>=0?'#34d399':'#f87171';

    // Try to find scanner analysis for this position
    let scanData=(_data&&_data.results)?_data.results[p.symbol]:null;
    let signal=scanData?scanData.signal:'HOLD';
    let signalLabel=scanData?(scanData.signal_label||signal):signal;
    let strength=scanData?(scanData.strength||0):0;
    let condsMet=scanData?(scanData.conditions_met||0):0;
    let verdict=signal==='SELL'?'SELL':(signal==='BUY'?'ADD':'HOLD');
    let cls=_portVerdictClass(verdict);
    let label=_portVerdictLabel(verdict);

    let indi='';
    if(scanData){
      indi+='<span class="port-verdict-indi-chip '+(scanData.macd_ok?'ok':'no')+'">MACD</span>';
      indi+='<span class="port-verdict-indi-chip '+(scanData.rsi_ok?'ok':'no')+'">RSI</span>';
      indi+='<span class="port-verdict-indi-chip '+(scanData.konc_ok?'ok':'no')+'">KONC</span>';
    }

    vHtml+='<div class="port-verdict-card '+cls+'">';
    vHtml+='  <div class="port-verdict-head">';
    vHtml+='    <div><div class="port-verdict-sym">'+p.symbol+'</div><div class="port-verdict-sub">'+(p.secType||'STK')+'</div></div>';
    vHtml+='    <span class="port-verdict-action '+cls+'">'+label+'</span>';
    vHtml+='  </div>';
    vHtml+='  <div class="port-verdict-metrics">';
    vHtml+='    <span>P&L <b style="color:'+pnlC+'">'+(pnlPct>=0?'+':'')+pnlPct.toFixed(1)+'%</b></span>';
    vHtml+='    <span>Fuerza <b>'+strength.toFixed(1)+'</b></span>';
    vHtml+='    <span>Indi <b>'+condsMet+'/3</b></span>';
    vHtml+='  </div>';
    if(indi)vHtml+='  <div class="port-verdict-indi">'+indi+'</div>';
    vHtml+='</div>';
  }
  document.getElementById('port-verdicts').innerHTML=vHtml;
  document.getElementById('port-analysis-list').innerHTML='';
}

// ═══════════════════════════════════════════
//  FORMATTERS
// ═══════════════════════════════════════════
function fmtN(n){
  if(n==null)return'---';
  return Number(n).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
}
function badge(s,label){
  let text=label||s;
  if(s==="BUY"){
    let cls=text.includes('FUERTE')?'b-buy-strong':'b-buy';
    return'<span class="badge '+cls+'">'+text+'</span>';
  }
  if(s==="SELL"){
    let cls=text.includes('FUERTE')?'b-sell-strong':'b-sell';
    return'<span class="badge '+cls+'">'+text+'</span>';
  }
  if(!label||label==='HOLD')return'<span class="badge b-hold">NEUTRAL</span>';
  if(label.includes('INMINENTE')&&label.includes('COMPRA'))return'<span class="badge b-buy-near">'+text+'</span>';
  if(label.includes('INMINENTE')&&label.includes('VENTA'))return'<span class="badge b-sell-near">'+text+'</span>';
  if(label.includes('VIRANDO')&&label.includes('COMPRA'))return'<span class="badge b-turning-buy">'+text+'</span>';
  if(label.includes('VIRANDO')&&label.includes('VENTA'))return'<span class="badge b-turning-sell">'+text+'</span>';
  if(label.includes('SOBREVENTA'))return'<span class="badge b-oversold">'+text+'</span>';
  if(label.includes('SOBRECOMPRA'))return'<span class="badge b-overbought">'+text+'</span>';
  return'<span class="badge b-hold">'+text+'</span>';
}
function fp(p){return p!=null?"$"+p.toFixed(2):"---";}
function fv(val,ok){
  if(val==null||(typeof val==='number'&&isNaN(val)))return'<span class="iv v-na">---</span>';
  return'<span class="iv '+(ok?'v-ok':'v-no')+'">'+val.toFixed(1)+'</span>';
}
function cc(n){return"cond cond-"+n;}
function fstr(val,sig,r){
  if(val==null||val===0)return'<span class="iv v-na">---</span>';
  let cls='v-na';
  if(sig==='BUY')cls=val>=3?'v-ok':'v-warn';
  else if(sig==='SELL')cls=val>=3?'v-no':'v-warn';
  else cls=val>=3?'v-warn':'v-na';
  return'<span class="iv '+cls+'">'+val.toFixed(1)+'</span>';
}
function sd(d){let s=String(d).replace(/-/g,"").replace(/ .*/,"");if(s.length>=8)return s.slice(6,8)+"/"+s.slice(4,6);return String(d).slice(-5);}

// ═══════════════════════════════════════════
//  SORTING
// ═══════════════════════════════════════════
let _sortCol='strength';
let _sortDir='desc';

function _getSortVal(r,col){
  if(!r)return null;
  let vals=r.values||{};
  let konc=vals.koncorde||{};
  switch(col){
    case 'sym':return r.symbol||'';
    case 'price':return r.price||0;
    case 'signal':{let l=r.signal_label||'';if(r.signal==='BUY')return l.includes('FUERTE')?7:6;if(r.signal==='SELL')return l.includes('FUERTE')?5:4;if(l.includes('INMINENTE'))return 3;if(l.includes('VIRANDO'))return 2;return 1;}
    case 'strength':return r.strength||0;
    case 'macd':return vals.macd?vals.macd.hist:null;
    case 'rsi':return vals.rsi!=null?vals.rsi:null;
    case 'konc':return konc.marron!=null?konc.marron:null;
    case 'cond':return r.conditions_met||0;
    default:return 0;
  }
}

function sortEntries(entries){
  return Object.keys(entries).sort((a,b)=>{
    let ra=entries[a],rb=entries[b];
    if(!ra&&!rb)return 0;if(!ra)return 1;if(!rb)return-1;
    let va=_getSortVal(ra,_sortCol);
    let vb=_getSortVal(rb,_sortCol);
    if(va==null&&vb==null)return 0;if(va==null)return 1;if(vb==null)return-1;
    let cmp;
    if(_sortCol==='sym')cmp=va.localeCompare(vb);
    else cmp=va-vb;
    return _sortDir==='asc'?cmp:-cmp;
  });
}

function sortListBy(col){
  if(_sortCol===col){_sortDir=_sortDir==='asc'?'desc':'asc';}
  else{_sortCol=col;_sortDir=(col==='sym'?'asc':'desc');}
  document.querySelectorAll('#list-header .sort-arrow').forEach(e=>e.remove());
  let span=document.querySelector('#list-header [data-col="'+col+'"]');
  if(span){
    let arrow=document.createElement('span');
    arrow.className='sort-arrow';
    arrow.textContent=_sortDir==='asc'?'▲':'▼';
    span.appendChild(arrow);
  }
  if(_data)update();
}
function sl(arr,n){if(!arr||arr.length<=n)return arr;return arr.slice(-n);}

// ═══════════════════════════════════════════
//  WEEKLY AGGREGATION
// ═══════════════════════════════════════════
function toWeekly(ohlc){
  if(!ohlc||ohlc.length===0)return[];
  let weeks=[],cur=null;
  for(let b of ohlc){
    let d=new Date(b.time+'T00:00:00');
    let day=d.getDay();let mon=new Date(d);mon.setDate(d.getDate()-(day===0?6:day-1));
    let wk=mon.toISOString().slice(0,10);
    if(!cur||cur.wk!==wk){
      if(cur)weeks.push(cur.bar);
      cur={wk:wk,bar:{time:b.time,open:b.open,high:b.high,low:b.low,close:b.close}};
    }else{
      cur.bar.high=Math.max(cur.bar.high,b.high);
      cur.bar.low=Math.min(cur.bar.low,b.low);
      cur.bar.close=b.close;
    }
  }
  if(cur)weeks.push(cur.bar);
  return weeks;
}

// ═══════════════════════════════════════════
//  CHART MANAGEMENT
// ═══════════════════════════════════════════
function destroyDetailCharts(idx){
  let c=_charts[idx];if(!c)return;
  if(c.lw)c.lw.remove();if(c.macd)c.macd.destroy();
  if(c.rsi)c.rsi.destroy();if(c.konc)c.konc.destroy();
  delete _charts[idx];
}

function _createLW(el,timeVis){
  return LightweightCharts.createChart(el,{
    width:el.clientWidth,height:310,
    layout:{background:{color:'#0d0d18'},textColor:'#94a3b8',fontSize:10,fontFamily:"'Inter',system-ui,sans-serif"},
    grid:{vertLines:{color:'#2a2a4a'},horzLines:{color:'#2a2a4a'}},
    crosshair:{mode:0,vertLine:{color:'#818cf877',labelBackgroundColor:'#818cf8'},horzLine:{color:'#818cf877',labelBackgroundColor:'#818cf8'}},
    timeScale:{borderColor:'#303055',timeVisible:!!timeVis},
    rightPriceScale:{borderColor:'#303055'},
  });
}

function renderCandleDaily(containerId,allOhlc,bars){
  let el=document.getElementById(containerId);if(!el)return null;
  let o=sl(allOhlc,bars);
  let chart=_createLW(el,false);
  let cs=chart.addCandlestickSeries({upColor:'#34d399',downColor:'#f87171',borderUpColor:'#34d399',borderDownColor:'#f87171',wickUpColor:'#34d39999',wickDownColor:'#f8717199'});
  cs.setData(o);
  chart.timeScale().fitContent();
  return chart;
}

function renderCandleWeekly(containerId,allOhlc){
  let el=document.getElementById(containerId);if(!el)return null;
  let weekly=toWeekly(allOhlc);
  let chart=_createLW(el,false);
  let cs=chart.addCandlestickSeries({upColor:'#34d399',downColor:'#f87171',borderUpColor:'#34d399',borderDownColor:'#f87171',wickUpColor:'#34d39999',wickDownColor:'#f8717199'});
  cs.setData(weekly);
  chart.timeScale().fitContent();
  return chart;
}

function renderCandleIntraday(containerId,ohlc){
  let el=document.getElementById(containerId);if(!el)return null;
  let chart=_createLW(el,true);
  let cs=chart.addCandlestickSeries({upColor:'#34d399',downColor:'#f87171',borderUpColor:'#34d399',borderDownColor:'#f87171',wickUpColor:'#34d39999',wickDownColor:'#f8717199'});
  cs.setData(ohlc);
  chart.timeScale().fitContent();
  return chart;
}

function createMACDChart(id,dates,macd,bars){
  let ctx=document.getElementById(id);if(!ctx)return null;
  let d=sl(dates,bars),m={hist:sl(macd.hist,bars),macd:sl(macd.macd,bars),signal:sl(macd.signal,bars)};
  let labels=d.map(sd);
  let colors=m.hist.map(v=>v>=0?'#10b981':'#ef4444');
  return new Chart(ctx,{type:'bar',data:{labels:labels,datasets:[
    {type:'bar',data:m.hist,backgroundColor:colors,borderWidth:0,barPercentage:.8,order:2},
    {type:'line',data:m.macd,borderColor:'#7dd3fc',borderWidth:1.5,pointRadius:0,fill:false,order:1},
    {type:'line',data:m.signal,borderColor:'#fb923c',borderWidth:1.5,pointRadius:0,fill:false,order:1}
  ]},options:{responsive:true,maintainAspectRatio:false,animation:false,
    plugins:{legend:{display:false}},
    scales:{x:{ticks:{color:'#8896a8',font:{size:8},maxTicksLimit:8},grid:{color:'#2a2a4a'}},
            y:{ticks:{color:'#8896a8',font:{size:8}},grid:{color:'#2a2a4a'}}}}});
}

function createRSIChart(id,dates,rsi,bars){
  let ctx=document.getElementById(id);if(!ctx)return null;
  let d=sl(dates,bars),r=sl(rsi,bars);
  let labels=d.map(sd);
  return new Chart(ctx,{type:'line',data:{labels:labels,datasets:[
    {data:r,borderColor:'#c084fc',borderWidth:2,pointRadius:0,fill:false}
  ]},options:{responsive:true,maintainAspectRatio:false,animation:false,
    plugins:{legend:{display:false}},
    scales:{x:{ticks:{color:'#8896a8',font:{size:8},maxTicksLimit:8},grid:{color:'#2a2a4a'}},
            y:{min:0,max:100,ticks:{color:'#8896a8',font:{size:8},stepSize:10},
               grid:{color:function(c){return(c.tick.value===30||c.tick.value===70)?'#ffffff33':'#2a2a4a';}}}}},
  plugins:[{id:'rz',beforeDraw(ch){
    let{ctx,chartArea:a,scales}=ch;if(!a)return;let y=scales.y;
    ctx.save();
    ctx.fillStyle='rgba(52,211,153,0.08)';ctx.fillRect(a.left,y.getPixelForValue(30),a.width,y.getPixelForValue(0)-y.getPixelForValue(30));
    ctx.fillStyle='rgba(248,113,113,0.08)';ctx.fillRect(a.left,y.getPixelForValue(100),a.width,y.getPixelForValue(70)-y.getPixelForValue(100));
    ctx.strokeStyle='#34d39950';ctx.setLineDash([4,4]);ctx.beginPath();ctx.moveTo(a.left,y.getPixelForValue(30));ctx.lineTo(a.right,y.getPixelForValue(30));ctx.stroke();
    ctx.strokeStyle='#f8717150';ctx.beginPath();ctx.moveTo(a.left,y.getPixelForValue(70));ctx.lineTo(a.right,y.getPixelForValue(70));ctx.stroke();
    ctx.restore();
  }}]});
}

function createKoncordeChart(id,dates,k,bars){
  let ctx=document.getElementById(id);if(!ctx)return null;
  let d=sl(dates,bars),kk={verde:sl(k.verde,bars),marron:sl(k.marron,bars),azul:sl(k.azul,bars),media:sl(k.media,bars)};
  let labels=d.map(sd);
  let marronBg=kk.marron.map(v=>v>=0?'rgba(251,191,36,0.55)':'rgba(251,191,36,0.38)');
  let verdeBg=kk.verde.map(v=>v>=0?'rgba(52,211,153,0.65)':'rgba(52,211,153,0.45)');
  return new Chart(ctx,{type:'bar',data:{labels:labels,datasets:[
    {type:'bar',label:'Marron',data:kk.marron,backgroundColor:marronBg,borderColor:'#fbbf24',borderWidth:0.5,barPercentage:0.95,categoryPercentage:0.95,stack:'s1',order:4},
    {type:'bar',label:'Verde',data:kk.verde,backgroundColor:verdeBg,borderColor:'#34d399',borderWidth:0.5,barPercentage:0.7,categoryPercentage:0.7,stack:'s2',order:3},
    {type:'line',label:'Azul',data:kk.azul,borderColor:'#60a5fa',borderWidth:2,pointRadius:0,fill:false,order:1},
    {type:'line',label:'Media',data:kk.media,borderColor:'#f87171',borderWidth:1.5,borderDash:[4,4],pointRadius:0,fill:false,order:0}
  ]},options:{responsive:true,maintainAspectRatio:false,animation:false,
    plugins:{legend:{display:true,position:'top',labels:{color:'#64748b',font:{size:9},boxWidth:10,padding:5}}},
    scales:{
      x:{stacked:true,ticks:{color:'#8896a8',font:{size:8},maxTicksLimit:8},grid:{color:'#2a2a4a'}},
      y:{stacked:false,ticks:{color:'#8896a8',font:{size:8}},grid:{color:'#2a2a4a'}}
    }},
  plugins:[{id:'zl',beforeDraw(ch){let{ctx,chartArea:a,scales}=ch;if(!a)return;let y0=scales.y.getPixelForValue(0);
    if(y0>=a.top&&y0<=a.bottom){ctx.save();ctx.strokeStyle='#ffffff28';ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(a.left,y0);ctx.lineTo(a.right,y0);ctx.stroke();ctx.restore();}}}]});
}

// ═══════════════════════════════════════════
//  RENDER CHARTS FOR A DETAIL
// ═══════════════════════════════════════════
function renderDetailCharts(idx,sym,period){
  if(!_data)return;let r=_data.results[sym];if(!r||!r.chart)return;
  destroyDetailCharts(idx);
  let ch=r.chart;
  let indBars=DAILY_BARS[period]||252;

  if(period==='5Y'){
    _charts[idx]={lw:renderCandleWeekly('candle_'+idx,ch.ohlc)};
  }else if(period==='ALL'){
    _charts[idx]={lw:renderCandleDaily('candle_'+idx,ch.ohlc,ch.ohlc.length)};
  }else if(period==='1M'||period==='1D'){
    let apiP=period==='1M'?'4h':'15m';
    let cacheKey=sym+'_'+apiP;
    if(_intradayCache[cacheKey]){
      _charts[idx]={lw:renderCandleIntraday('candle_'+idx,_intradayCache[cacheKey])};
    }else{
      let el=document.getElementById('candle_'+idx);
      if(el)el.innerHTML='<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--muted);font-size:13px">Cargando barras '+apiP+'...</div>';
      authFetch('/api/bars/'+sym+'/'+apiP).then(r=>r.json()).then(d=>{
        _intradayCache[cacheKey]=d.ohlc||d.bars||[];
        if(_periods[idx]===period){
          let el2=document.getElementById('candle_'+idx);
          if(el2)el2.innerHTML='';
          if(!_charts[idx])_charts[idx]={};
          if(_charts[idx].lw)_charts[idx].lw.remove();
          _charts[idx].lw=renderCandleIntraday('candle_'+idx,_intradayCache[cacheKey]);
        }
      }).catch(e=>console.error('Intraday fetch error:',e));
      _charts[idx]={};
    }
  }else{
    let bars=DAILY_BARS[period]||252;
    _charts[idx]={lw:renderCandleDaily('candle_'+idx,ch.ohlc,bars)};
  }

  let c=_charts[idx]||{};
  c.macd=createMACDChart('macd_'+idx,ch.dates,ch.macd,indBars);
  c.rsi=createRSIChart('rsi_'+idx,ch.dates,ch.rsi,indBars);
  c.konc=createKoncordeChart('konc_'+idx,ch.dates,ch.koncorde,indBars);
  _charts[idx]=c;
}

function setPeriod(idx,sym,period){
  _periods[idx]=period;
  let btns=document.querySelectorAll('#pb_'+idx+' .period-btn');
  btns.forEach(b=>{b.classList.toggle('active',b.dataset.p===period);});
  renderDetailCharts(idx,sym,period);
}

// ═══════════════════════════════════════════
//  TOP 3 RECOMMENDATIONS
// ═══════════════════════════════════════════
let _recDetailCharts={};
let _recResizeObservers={};
let _top3Data=[];
let _recPeriods={};
let _recIntradayCache={};

function destroyRecDetailCharts(idx){
  let entry=_recDetailCharts[idx];
  if(!entry)return;
  try{if(entry.lw)entry.lw.remove();}catch(e){}
  try{if(entry.macd)entry.macd.destroy();}catch(e){}
  try{if(entry.rsi)entry.rsi.destroy();}catch(e){}
  try{if(entry.konc)entry.konc.destroy();}catch(e){}
  let ro=_recResizeObservers[idx];
  if(ro)try{ro.disconnect();}catch(e){}
  delete _recDetailCharts[idx];
  delete _recResizeObservers[idx];
}

function destroyAllRecCharts(){
  for(let idx in _recDetailCharts)destroyRecDetailCharts(idx);
  _recDetailCharts={};
  _recResizeObservers={};
}

function renderRecDetailCharts(idx,rec,period){
  destroyRecDetailCharts(idx);
  if(!rec)return;
  let sym=rec.symbol;
  if(!period)period=_recPeriods[idx]||'1Y';
  _recPeriods[idx]=period;

  let bar=document.getElementById('rec_pb_'+idx);
  if(bar){bar.querySelectorAll('.rec-period-btn').forEach(b=>{b.classList.toggle('active',b.dataset.p===period);});}

  let entry={lw:null,macd:null,rsi:null,konc:null};

  let fullData=(_data&&_data.results)?_data.results[sym]:null;
  let ch=fullData?fullData.chart:null;
  let indBars=DAILY_BARS[period]||252;

  let candleEl=document.getElementById('rec_candle_'+idx);
  if(candleEl){
    candleEl.innerHTML='';
    if(ch&&ch.ohlc&&ch.ohlc.length>=5){
      let chart=_createLW(candleEl,false);chart.applyOptions({height:360});
      let cs=chart.addCandlestickSeries({upColor:'#34d399',downColor:'#f87171',borderUpColor:'#34d399',borderDownColor:'#f87171',wickUpColor:'#34d39988',wickDownColor:'#f8717188'});
      if(period==='5Y'){
        let weekly=toWeekly(ch.ohlc);cs.setData(weekly);
      }else if(period==='ALL'){
        cs.setData(ch.ohlc);
      }else{
        let o=sl(ch.ohlc,indBars);cs.setData(o);
      }
      chart.timeScale().fitContent();
      entry.lw=chart;
      let ro=new ResizeObserver(()=>{chart.applyOptions({width:candleEl.clientWidth});});ro.observe(candleEl);_recResizeObservers[idx]=ro;
    }else{
      candleEl.innerHTML='<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--muted);font-size:12px">Sin datos historicos disponibles.</div>';
    }
  }

  let dates=ch?ch.dates:null;
  let macdData=ch?ch.macd:null;
  let rsiData=ch?ch.rsi:null;
  let koncData=ch?ch.koncorde:null;
  if(dates&&dates.length>0){
    if(macdData)entry.macd=createMACDChart('rec_macd_'+idx,dates,macdData,indBars);
    if(rsiData&&rsiData.length>0)entry.rsi=createRSIChart('rec_rsi_'+idx,dates,rsiData,indBars);
    if(koncData)entry.konc=createKoncordeChart('rec_konc_'+idx,dates,koncData,indBars);
  }

  _recDetailCharts[idx]=entry;
}

function setRecPeriod(idx,period){
  _recPeriods[idx]=period;
  let rec=_top3Data[idx];
  if(!rec)return;
  renderRecDetailCharts(idx,rec,period);
}

let _recFirstRender=true;
function renderTop3(top3){
  let recOpenSet=new Set();
  document.querySelectorAll('.rec-details[open]').forEach(d=>{let idx=d.dataset.idx;if(idx!=null)recOpenSet.add(parseInt(idx));});
  destroyAllRecCharts();
  _top3Data=top3||[];
  let sec=document.getElementById('top3-section');
  if(!sec)return;
  if(!top3||top3.length===0){sec.style.display='none';return;}
  sec.style.display='';

  let periods=['1Y','3M','1W'];
  let html='<div class="top3-title">Top Recomendaciones</div>';
  for(let i=0;i<top3.length;i++){
    let r=top3[i];
    let sc=r.signal==='BUY'?'rec-buy':(r.signal==='SELL'?'rec-sell':'rec-hold');
    let slabel=r.signal_label||r.signal;
    let bc=r.signal==='BUY'?'rb-buy':(r.signal==='SELL'?'rb-sell':(slabel.includes('INMINENTE')&&slabel.includes('COMPRA')?'rb-buy-near':(slabel.includes('INMINENTE')&&slabel.includes('VENTA')?'rb-sell-near':'rb-hold')));
    let curP=_recPeriods[i]||'1Y';

    let shouldOpen=_recFirstRender?(i===0):recOpenSet.has(i);
    html+='<details class="rec-details '+sc+'"'+(shouldOpen?' open':'')+' data-idx="'+i+'">';
    html+='<summary>';
    html+='<span class="rec-arrow">&#9654;</span>';
    html+='<span class="rec-rank-badge">#'+(i+1)+'</span>';
    html+='<span class="rec-sym">'+r.symbol+'</span>';
    html+='<span class="rec-price">$'+(r.price||0).toFixed(2)+'</span>';
    html+='<span class="rec-badge '+bc+'">'+slabel+'</span>';
    html+='<span class="rec-sum-metrics">';
    html+='<span class="rec-sm"><span class="lab">Fuerza</span><span class="val" style="color:'+(r.strength>=3?'var(--buy)':'var(--hold)')+'">'+((r.strength||0).toFixed(1))+'</span></span>';
    html+='<span class="rec-sm"><span class="lab">Indi</span><span class="val">'+(r.conditions_met||0)+'/3</span></span>';
    html+='</span>';
    html+='</summary>';

    html+='<div class="rec-body">';

    // Period buttons
    html+='<div class="rec-period-bar" id="rec_pb_'+i+'">';
    for(let p of periods){
      html+='<button class="rec-period-btn'+(p===curP?' active':'')+'" data-p="'+p+'" onclick="setRecPeriod('+i+',\''+p+'\')">'+p+'</button>';
    }
    html+='</div>';

    // Candle chart
    html+='<div class="rec-candle-wrap"><div class="rec-candle-box" id="rec_candle_'+i+'"></div></div>';

    // Indicator row
    html+='<div class="rec-ind-grid">';
    html+='<div class="rec-ind-wrap"><div class="rec-ind-title">MACD (12,26,9)</div><div style="padding:4px"><canvas class="rec-ind-canvas" id="rec_macd_'+i+'"></canvas></div></div>';
    html+='<div class="rec-ind-wrap"><div class="rec-ind-title">RSI (14)</div><div style="padding:4px"><canvas class="rec-ind-canvas" id="rec_rsi_'+i+'"></canvas></div></div>';
    html+='<div class="rec-ind-wrap"><div class="rec-ind-title">Koncorde</div><div style="padding:4px"><canvas class="rec-ind-canvas" id="rec_konc_'+i+'"></canvas></div></div>';
    html+='</div>';

    // Condition details
    html+='<div style="margin-top:10px">';
    html+='<div class="cond-line"><span class="cond-label" style="color:#7dd3fc">MACD</span><span class="'+(r.macd_ok?'v-ok':'v-no')+'">'+(r.macd_detail||'---')+'</span></div>';
    html+='<div class="cond-line"><span class="cond-label" style="color:#c084fc">RSI</span><span class="'+(r.rsi_ok?'v-ok':'v-no')+'">'+(r.rsi_detail||'---')+'</span></div>';
    html+='<div class="cond-line"><span class="cond-label" style="color:#fbbf24">Koncorde</span><span class="'+(r.konc_ok?'v-ok':'v-no')+'">'+(r.konc_detail||'---')+'</span></div>';
    html+='</div>';

    html+='</div>';
    html+='</details>';
  }

  sec.innerHTML=html;

  sec.querySelectorAll('.rec-details').forEach(det=>{
    let idx=parseInt(det.dataset.idx);
    det.addEventListener('toggle',function(){
      if(det.open){
        setTimeout(()=>renderRecDetailCharts(idx,_top3Data[idx]),50);
      }else{
        destroyRecDetailCharts(idx);
      }
    });
    if(det.open){
      setTimeout(()=>renderRecDetailCharts(idx,_top3Data[idx]),80);
    }
  });
  _recFirstRender=false;
}

// ═══════════════════════════════════════════
//  MAIN UPDATE
// ═══════════════════════════════════════════
function update(){
  let scrollY=window.scrollY;
  authFetch("/api/data").then(r=>r.json()).then(data=>{
    _data=data;
    document.getElementById("last-update").textContent=data.last_update||"--";

    let entries=data.results||{};
    let buy=0,sell=0,buyNear=0,sellNear=0,turnBuy=0,turnSell=0,zone=0,neutral=0,nodata=0,total=Object.keys(entries).length;
    for(let s in entries){
      let r=entries[s];
      if(!r){nodata++;continue;}
      if(r.signal==='BUY')buy++;
      else if(r.signal==='SELL')sell++;
      else{
        let l=r.signal_label||'';
        if(l.includes('INMINENTE')&&l.includes('COMPRA'))buyNear++;
        else if(l.includes('INMINENTE')&&l.includes('VENTA'))sellNear++;
        else if(l.includes('VIRANDO')&&l.includes('COMPRA'))turnBuy++;
        else if(l.includes('VIRANDO')&&l.includes('VENTA'))turnSell++;
        else if(l.includes('SOBREVENTA')||l.includes('SOBRECOMPRA'))zone++;
        else neutral++;
      }
    }

    if(total===0&&!_bridgeConnected){
      document.getElementById("counters").innerHTML='<span class="counter c-nodata">Conecta TWS para ver datos</span>';
      document.getElementById("stock-list").innerHTML='<div style="text-align:center;color:var(--dim);padding:60px"><p style="font-size:16px;margin-bottom:8px">TWS no conectado</p><p>Conecta tu TWS usando la pestana "Conectar TWS"</p></div>';
      document.getElementById("top3-section").style.display='none';
      requestAnimationFrame(()=>{window.scrollTo(0,scrollY);});
      return;
    }

    let ch='<span class="counter c-total">Total '+total+'</span>';
    if(buy)ch+='<span class="counter c-buy">Compra '+buy+'</span>';
    if(sell)ch+='<span class="counter c-sell">Venta '+sell+'</span>';
    if(buyNear)ch+='<span class="counter c-buy-near">Compra Inminente '+buyNear+'</span>';
    if(sellNear)ch+='<span class="counter c-sell-near">Venta Inminente '+sellNear+'</span>';
    if(turnBuy)ch+='<span class="counter c-turning-buy">Virando a Compra '+turnBuy+'</span>';
    if(turnSell)ch+='<span class="counter c-turning-sell">Virando a Venta '+turnSell+'</span>';
    if(zone)ch+='<span class="counter c-zone">Zona Extrema '+zone+'</span>';
    if(neutral)ch+='<span class="counter c-neutral">Neutral '+neutral+'</span>';
    if(nodata)ch+='<span class="counter c-nodata">Sin datos '+nodata+'</span>';
    document.getElementById("counters").innerHTML=ch;

    renderTop3(data.top3);

    let sorted=sortEntries(entries);
    let openSet=new Set();
    document.querySelectorAll('details[open]').forEach(d=>{if(d.dataset.sym)openSet.add(d.dataset.sym);});
    for(let k in _charts)destroyDetailCharts(k);

    let html="";
    let idx=0;
    for(let sym of sorted){
      let r=entries[sym];
      if(!r){
        let na='<span class="iv v-na">---</span>';
        html+='<details data-sym="'+sym+'" data-idx="'+idx+'"><summary>'+
          '<div class="stock-row">'+
          '<span class="arrow">&#9654;</span><span class="sym">'+sym+'</span>'+
          '<span class="price" style="color:var(--dim)">---</span><span></span>'+na+
          na+na+na+na+na+na+na+na+na+na+na+na+
          '</div></summary>'+
          '<div class="detail-body" style="color:var(--dim)">Sin datos historicos</div></details>';
        idx++;continue;
      }

      let cond=r.conditions_met||0;
      let isOpen=openSet.has(sym);
      let mh=r.values&&r.values.macd?r.values.macd.hist:null;
      let rv=r.values?r.values.rsi:null;
      let km=r.values&&r.values.koncorde?r.values.koncorde.marron:null;

      html+='<details data-sym="'+sym+'" data-idx="'+idx+'"'+(isOpen?' open':'')+'>';
      html+='<summary><div class="stock-row">'+
        '<span class="arrow">&#9654;</span>'+
        '<span class="sym">'+sym+'</span>'+
        '<span class="price">'+fp(r.price)+'</span>'+
        badge(r.signal,r.signal_label)+fstr(r.strength,r.signal,r)+
        fv(mh,r.macd_ok)+fv(rv,r.rsi_ok)+fv(km,r.konc_ok)+
        '<span class="'+cc(cond)+'">'+cond+'/3</span>'+
        '<span></span><span></span><span></span><span></span><span></span><span></span><span></span><span></span>'+
        '</div>';
      html+='</summary>';

      let curPeriod=_periods[idx]||'1Y';
      html+='<div class="detail-body">';
      html+=''+
        '<div class="cond-line"><span class="cond-label" style="color:#7dd3fc">MACD</span><span class="'+(r.macd_ok?'v-ok':'v-no')+'">'+(r.macd_detail||"---")+'</span></div>'+
        '<div class="cond-line"><span class="cond-label" style="color:#c084fc">RSI</span><span class="'+(r.rsi_ok?'v-ok':'v-no')+'">'+(r.rsi_detail||"---")+'</span></div>'+
        '<div class="cond-line"><span class="cond-label" style="color:#fbbf24">Koncorde</span><span class="'+(r.konc_ok?'v-ok':'v-no')+'">'+(r.konc_detail||"---")+'</span></div>';

      if(r.chart&&r.chart.ohlc&&r.chart.ohlc.length>0){
        html+='<div style="display:flex;justify-content:flex-end;margin:10px 0 4px">'+
          '<div class="period-bar" id="pb_'+idx+'">'+
          '<button class="period-btn'+(curPeriod==='1Y'?' active':'')+'" data-p="1Y" onclick="setPeriod('+idx+',\''+sym+'\',\'1Y\')">1Y</button>'+
          '<button class="period-btn'+(curPeriod==='3M'?' active':'')+'" data-p="3M" onclick="setPeriod('+idx+',\''+sym+'\',\'3M\')">3M</button>'+
          '<button class="period-btn'+(curPeriod==='1W'?' active':'')+'" data-p="1W" onclick="setPeriod('+idx+',\''+sym+'\',\'1W\')">1W</button>'+
          '</div></div>'+
          '<div class="candle-box" id="candle_'+idx+'"></div>'+
          '<div class="charts-grid">'+
          '<div class="chart-box"><h4>MACD</h4><canvas id="macd_'+idx+'"></canvas></div>'+
          '<div class="chart-box"><h4>RSI</h4><canvas id="rsi_'+idx+'"></canvas></div>'+
          '<div class="chart-box"><h4>KONCORDE</h4><canvas id="konc_'+idx+'"></canvas></div>'+
          '</div>';
      }
      html+='</div></details>';
      idx++;
    }
    document.getElementById("stock-list").innerHTML=html;

    document.querySelectorAll('details[open]').forEach(d=>{
      let i=parseInt(d.dataset.idx);
      renderDetailCharts(i,d.dataset.sym,_periods[i]||'1Y');
    });
    document.querySelectorAll('details').forEach(d=>{
      d.addEventListener('toggle',function(){
        let i=parseInt(this.dataset.idx),s=this.dataset.sym;
        if(this.open)renderDetailCharts(i,s,_periods[i]||'1Y');else destroyDetailCharts(i);
      });
    });
    requestAnimationFrame(()=>{window.scrollTo(0,scrollY);});
  }).catch(err=>{
    if(err.message==='unauthorized')return;
    console.error("Error:",err);
  });
}

// ═══════════════════════════════════════════
//  INIT
// ═══════════════════════════════════════════
fetchBridgeToken();
fetchStatus();
update();
setInterval(fetchStatus,5000);
setInterval(update,REFRESH_MS);
setInterval(function(){if(_activeTab==='portfolio'&&_portLoaded){_portLoaded=false;loadPortfolio();}},REFRESH_MS*20);
</script>
</body>
</html>"""
