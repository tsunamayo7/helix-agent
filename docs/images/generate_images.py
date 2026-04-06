import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np
from pathlib import Path

# Output dir
out_dir = Path("C:/Development/tools/helix-agent/docs/images")
out_dir.mkdir(parents=True, exist_ok=True)

# Common theme
BG = '#1a1a2e'
TEXT = '#ffffff'
TEXT_LIGHT = '#cccccc'
RED = '#e74c3c'
RED_DARK = '#c0392b'
GREEN = '#2ecc71'
GREEN_DARK = '#27ae60'
ACCENT = '#f39c12'

plt.rcParams.update({
    'figure.facecolor': BG,
    'axes.facecolor': BG,
    'text.color': TEXT,
    'axes.labelcolor': TEXT,
    'xtick.color': TEXT_LIGHT,
    'ytick.color': TEXT_LIGHT,
    'font.family': 'sans-serif',
    'font.sans-serif': ['Segoe UI', 'Arial', 'DejaVu Sans'],
})

# ============================================================
# IMAGE 1: before_after_tokens.png
# ============================================================
def make_before_after():
    fig, ax = plt.subplots(figsize=(12, 6.75), dpi=100)

    tasks = [
        'Retry loop\n(worst case)',
        'Browser\nautomation',
        'DOM/HTML\nprocessing',
        'Screenshot\nanalysis',
    ]
    before = [600_000, 15_000, 114_000, 15_000]
    after  = [45_000,  2_100,  500,     400]
    savings = ['93%', '86%', '99%', '97%']

    y = np.arange(len(tasks))
    bar_h = 0.35

    bars_before = ax.barh(y + bar_h/2, before, bar_h, color=RED, alpha=0.85, label='Before', edgecolor='none')
    bars_after  = ax.barh(y - bar_h/2, after,  bar_h, color=GREEN, alpha=0.9, label='After (helix-agent)', edgecolor='none')

    ax.set_yticks(y)
    ax.set_yticklabels(tasks, fontsize=13, fontweight='bold')
    ax.set_xlabel('Tokens per task', fontsize=12)
    ax.set_xscale('log')
    ax.set_xlim(100, 1_500_000)

    # Savings annotations
    for i, (b, a, s) in enumerate(zip(before, after, savings)):
        ax.text(b * 1.15, i + bar_h/2, f'{b:,}', va='center', fontsize=10, color=TEXT_LIGHT)
        ax.text(max(a * 1.15, 200), i - bar_h/2, f'{a:,}', va='center', fontsize=10, color=TEXT_LIGHT)
        # Big savings badge
        ax.text(1_100_000, i, f'{s}\nsaved', va='center', ha='center',
                fontsize=16, fontweight='bold', color=GREEN,
                bbox=dict(boxstyle='round,pad=0.3', facecolor='#16a085', alpha=0.3, edgecolor=GREEN))

    ax.legend(fontsize=12, loc='lower right', framealpha=0.3, edgecolor='none')
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    ax.spines['bottom'].set_color(TEXT_LIGHT)
    ax.spines['left'].set_color(TEXT_LIGHT)

    ax.set_title('Token Savings with helix-agent', fontsize=22, fontweight='bold', pad=20, color=TEXT)
    fig.text(0.5, 0.02, 'All compression runs locally via Ollama ($0)',
             ha='center', fontsize=14, color=ACCENT, style='italic')

    plt.tight_layout(rect=[0, 0.06, 1, 1])
    fig.savefig(out_dir / 'before_after_tokens.png', dpi=100, facecolor=BG)
    plt.close()
    print(f"Created: {out_dir / 'before_after_tokens.png'}")

# ============================================================
# IMAGE 2: token_breakdown_pie.png
# ============================================================
def make_pie():
    fig, ax = plt.subplots(figsize=(12, 6.75), dpi=100)

    labels = ['System prompt +\ntool schemas', 'Screenshot /\nDOM', 'Conversation\nhistory', 'Your actual\nprompt']
    sizes = [56, 31, 12, 1]
    tokens = ['45,000', '25,000', '9,500', '500']

    colors_pie = ['#34495e', '#2c3e50', '#3d566e', '#f1c40f']
    explode = (0, 0, 0, 0.15)

    wedges, texts, autotexts = ax.pie(
        sizes, labels=None, autopct='%1.0f%%', startangle=90,
        colors=colors_pie, explode=explode, pctdistance=0.78,
        textprops={'fontsize': 13, 'color': TEXT, 'fontweight': 'bold'},
        wedgeprops={'edgecolor': BG, 'linewidth': 2}
    )

    # Make "Your prompt" percentage bright
    autotexts[-1].set_color('#f1c40f')
    autotexts[-1].set_fontsize(15)

    # Legend
    legend_labels = [f'{l}  ({t} tokens)' for l, t in zip(
        ['System prompt + tool schemas', 'Screenshot / DOM', 'Conversation history', 'Your actual prompt'],
        tokens
    )]
    ax.legend(wedges, legend_labels, loc='center left', bbox_to_anchor=(0.85, 0.5),
              fontsize=10, framealpha=0.3, edgecolor='none',
              labelcolor=TEXT)

    # Center text
    ax.text(0, 0, 'Your prompt:\n<1%', ha='center', va='center',
            fontsize=18, fontweight='bold', color='#f1c40f')

    ax.set_title('Where Your Claude Code Tokens Go', fontsize=22, fontweight='bold', pad=20, color=TEXT)
    fig.text(0.5, 0.03, '99% is overhead. helix-agent compresses the rest.',
             ha='center', fontsize=14, color=ACCENT, style='italic')

    plt.tight_layout(rect=[0, 0.07, 0.85, 1])
    fig.savefig(out_dir / 'token_breakdown_pie.png', dpi=100, facecolor=BG)
    plt.close()
    print(f"Created: {out_dir / 'token_breakdown_pie.png'}")

# ============================================================
# IMAGE 3: gpu_benchmark.png
# ============================================================
def make_gpu_table():
    fig, ax = plt.subplots(figsize=(12, 6.75), dpi=100)
    ax.axis('off')

    col_labels = ['GPU', 'VRAM', 'Model', 'DOM Compress', 'Speed vs 31b']
    data = [
        ['RTX 4060',     '8 GB',   'gemma4:e2b',  '10.2 s', '2.7x faster'],
        ['RTX 4070 Ti',  '16 GB',  'gemma4:e4b',  '11.8 s', '2.3x faster'],
        ['RTX 4090',     '24 GB',  'gemma4:26b',  '14.7 s', '1.9x faster'],
        ['RTX PRO 6000', '48 GB+', 'gemma4:31b',  '27.5 s', 'baseline'],
    ]

    table = ax.table(
        cellText=data,
        colLabels=col_labels,
        loc='center',
        cellLoc='center',
    )

    table.auto_set_font_size(False)
    table.set_fontsize(14)
    table.scale(1.0, 2.2)

    # Style all cells
    for (row, col), cell in table.get_celld().items():
        cell.set_edgecolor('#2d3436')
        cell.set_text_props(color=TEXT)

        if row == 0:
            # Header
            cell.set_facecolor('#0d6efd')
            cell.set_text_props(color=TEXT, fontweight='bold', fontsize=14)
            cell.set_height(0.12)
        elif row == 1:
            # 8GB row - highlighted
            cell.set_facecolor('#1a5c2a')
            cell.set_text_props(color=GREEN, fontweight='bold', fontsize=14)
        else:
            cell.set_facecolor('#2d2d44')
            cell.set_text_props(fontsize=13)

    # Make "2.7x faster" extra bold in highlighted row
    cell_speed = table[1, 4]
    cell_speed.set_text_props(color='#00ff88', fontweight='bold', fontsize=16)

    ax.set_title('Works on 8GB VRAM — No Expensive GPU Needed',
                 fontsize=22, fontweight='bold', pad=30, color=TEXT)
    fig.text(0.5, 0.06, 'helix-agent auto-detects your GPU and picks the best model',
             ha='center', fontsize=14, color=ACCENT, style='italic')

    # Add a "BEST VALUE" badge to the left
    ax.text(0.06, 0.58, 'BEST\nVALUE', ha='center', va='center',
            fontsize=13, fontweight='bold', color=BG,
            bbox=dict(boxstyle='round,pad=0.4', facecolor=GREEN, edgecolor='none'),
            transform=ax.transAxes)

    plt.tight_layout(rect=[0, 0.1, 1, 0.95])
    fig.savefig(out_dir / 'gpu_benchmark.png', dpi=100, facecolor=BG)
    plt.close()
    print(f"Created: {out_dir / 'gpu_benchmark.png'}")


if __name__ == '__main__':
    make_before_after()
    make_pie()
    make_gpu_table()
    print("\nAll 3 images generated successfully!")
