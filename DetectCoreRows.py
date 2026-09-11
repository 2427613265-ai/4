import numpy as np
import matplotlib.pyplot as plt
import matplotlib.patches as patches
from sklearn.cluster import DBSCAN, KMeans
from collections import Counter
import cv2
import os
from PIL import Image, ImageDraw, ImageFont
# from collections import defaultdict
# from CoreCardRun import runCoreCardModel_OutputBoxCoor
# from CoreRun import runCoreModel_OutputCoreBoxCoor
# import os
# from CorrectPerspective import correctPerspectiveImage

try:
    from .ModelRun import runCoreModel_OutputCoreBoxCoor
except ImportError:
    try:
        from ModelRun import runCoreModel_OutputCoreBoxCoor
    except ImportError:
        def runCoreModel_OutputCoreBoxCoor(*args, **kwargs):
            raise ImportError(
                "ModelRun.runCoreModel_OutputCoreBoxCoor is not available; "
                "pass detection boxes into the clustering functions directly."
            )

def detect_rows_from_boxes(boxes, eps_ratio=0.08, min_samples=2, img_height=None):
    """
    根据岩芯检测框判断岩芯箱内有多少行岩芯

    参数:
        boxes: 检测框列表，每个框为 [x1, y1, x2, y2] 格式（像素坐标）
        eps_ratio: 聚类邻域半径比例，默认0.08表示8%的图像高度
        min_samples: 形成核心点所需的最小样本数，默认为2
        img_height: 图像高度（像素）。给定时 eps = eps_ratio * img_height，
                    与“比例相对整图高度”的定义一致；缺省时退化为检测框 Y 跨度。

    返回:
        row_count: 行数
        row_labels: 每个检测框对应的行标签（0, 1, 2, ...），从上到下依次为第1行、第2行...
        cluster_centers: 每行的中心Y坐标（从上到下排列）
    """
    if len(boxes) == 0:
        return 0, [], []

    # 1. 提取每个框的中心点Y坐标
    centers_y = []
    for box in boxes:
        y = [i[1] for i in box]
        y1 = max(y)
        y2 = min(y)
        center_y = (y1 + y2) / 2.0
        centers_y.append(center_y)

    # 2. 计算自适应邻域半径
    if eps_ratio is None:
        box_heights = []
        for box in boxes:
            y = [i[1] for i in box]
            box_heights.append(max(y) - min(y))
        median_height = np.median(box_heights)
        eps = median_height * 1.2
    else:
        if img_height is None or img_height <= 0:
            all_y = []
            for box in boxes:
                y = [i[1] for i in box]
                all_y.extend([min(y), max(y)])
            img_height = max(all_y) - min(all_y) if all_y else 1000
        eps = eps_ratio * float(img_height)

    # 3. 将Y坐标转为二维数组
    X = np.array(centers_y).reshape(-1, 1)

    # 4. 执行DBSCAN聚类
    clustering = DBSCAN(eps=eps, min_samples=min_samples).fit(X)
    labels = clustering.labels_

    # 5. 统计行数（排除噪声点）
    unique_labels = set(labels)
    row_count = sum(1 for label in unique_labels if label != -1)

    # ========== 修改开始 ==========
    # 6. 计算每行的中心Y坐标（按Y从小到大排序）
    cluster_info = []  # 存储 (中心Y, 原始标签)
    for label in unique_labels:
        if label == -1:
            continue
        y_values = [centers_y[i] for i, l in enumerate(labels) if l == label]
        center_y = np.mean(y_values)
        cluster_info.append((center_y, label))

    # 按中心Y坐标从小到大排序（从上到下）
    cluster_info_sorted = sorted(cluster_info, key=lambda x: x[0])

    # 创建映射：原始标签 -> 新序号（0, 1, 2, ... 从上到下）
    label_mapping = {}
    for new_id, (center_y, old_label) in enumerate(cluster_info_sorted):
        label_mapping[old_label] = new_id

    # 应用映射，生成新的标签列表
    ordered_labels = []
    for l in labels:
        if l == -1:
            ordered_labels.append(-1)
        else:
            ordered_labels.append(label_mapping[l])

    # 按从上到下顺序返回聚类中心
    ordered_centers = [center_y for center_y, _ in cluster_info_sorted]
    # ========== 修改结束 ==========

    return row_count, ordered_labels, ordered_centers


def detect_rows_with_fixed_count(boxes, n_rows, init_centers=None):
    """
    将岩芯检测框按 Y 坐标聚类为**恰好** n_rows 行（用户指定行数时使用）。

    与 detect_rows_from_boxes（DBSCAN 自动判断行数）不同，此函数使用 KMeans
    强制分成 n_rows 簇，适合用户手动修正行数的场景。

    参数:
        boxes: 检测框列表，每个框为 [[x,y], ...] 格式（多边形顶点）
        n_rows: 用户指定的行数（>=1）
        init_centers: list[float] | None — 自定义初始簇中心 Y 坐标（从上到下），
                      长度应等于 n_rows；为 None 时使用 KMeans 默认随机初始化。

    返回:
        row_count: int                  — 实际行数（= n_rows，除非检测框数更少）
        row_labels: list[int]           — 每个检测框的行标签（0,1,...,从上到下）
        cluster_centers: list[float]    — 每行中心 Y 坐标（从上到下）
    """
    if len(boxes) == 0 or n_rows <= 0:
        return 0, [], []

    # 1. 提取每个框的中心点 Y 坐标
    centers_y = []
    for box in boxes:
        y = [i[1] for i in box]
        center_y = (max(y) + min(y)) / 2.0
        centers_y.append(center_y)

    # 检测框数 < 指定行数：每个框自成一行（截断到实际框数）
    actual_n = min(n_rows, len(boxes))
    if actual_n <= 1:
        return 1, [0] * len(boxes), [float(np.mean(centers_y))] if centers_y else []

    # 2. KMeans 聚类（1D Y 坐标）
    X = np.array(centers_y).reshape(-1, 1)

    # 如果提供了自定义初始中心且数量匹配，用它做初始化（n_init=1）
    use_custom_init = (
        init_centers is not None
        and len(init_centers) == actual_n
    )
    if use_custom_init:
        init_array = np.array(init_centers, dtype=float).reshape(-1, 1)
        kmeans = KMeans(n_clusters=actual_n, init=init_array, n_init=1, random_state=42)
    else:
        kmeans = KMeans(n_clusters=actual_n, random_state=42, n_init=10)
    raw_labels = kmeans.fit_predict(X)

    # 3. 按中心 Y 从小到大（从上到下）重排标签
    cluster_info = []  # (中心Y, 原始标签)
    for label in set(raw_labels):
        y_values = [centers_y[i] for i, l in enumerate(raw_labels) if l == label]
        center_y = float(np.mean(y_values))
        cluster_info.append((center_y, label))

    cluster_info_sorted = sorted(cluster_info, key=lambda x: x[0])

    label_mapping = {}
    for new_id, (center_y, old_label) in enumerate(cluster_info_sorted):
        label_mapping[old_label] = new_id

    ordered_labels = [label_mapping[l] for l in raw_labels]
    ordered_centers = [center_y for center_y, _ in cluster_info_sorted]

    return actual_n, ordered_labels, ordered_centers


def visualize_row_clustering(boxes, row_labels, row_centers, img_shape=None, title="岩芯行聚类结果"):
    """
    可视化聚类结果

    参数:
        boxes: 检测框列表
        row_labels: 每行标签
        row_centers: 每行中心Y坐标
        img_shape: 图像尺寸 (height, width)，可选
        title: 图表标题
    """
    # 确定图像尺寸
    if img_shape is not None:
        img_height, img_width = img_shape[:2]
    else:
        # 从检测框推算
        all_x = []
        all_y = []
        for box in boxes:
            x = [i[0] for i in box]
            y = [i[1] for i in box]
            all_x.extend([min(x), max(x)])
            all_y.extend([min(y), max(y)])
        img_width = max(all_x) - min(all_x) + 200 if all_x else 1000
        img_height = max(all_y) - min(all_y) + 200 if all_y else 1000
        # 防止负值
        img_width = max(img_width, 640)
        img_height = max(img_height, 480)

    # 创建子图
    fig, axes = plt.subplots(1, 2, figsize=(16, 7))

    # ===== 左图：显示原始检测框，按行着色 =====
    colors = ['#E74C3C', '#3498DB', '#2ECC71', '#F39C12', '#9B59B6',
              '#1ABC9C', '#E67E22', '#2C3E50', '#E84393', '#00B894']

    ax1 = axes[0]

    # 如果没有检测框，显示提示
    if len(boxes) == 0:
        ax1.text(0.5, 0.5, '没有检测到岩芯', ha='center', va='center', fontsize=16)
    else:
        for i, box in enumerate(boxes):
            x = [i[0] for i in box]
            y = [i[1] for i in box]
            x1, y1, x2, y2 = min(x), min(y), max(x), max(y)
            label = row_labels[i] if i < len(row_labels) else -1

            # 确保标签有效
            color = colors[label % len(colors)] if label != -1 else 'gray'

            # 绘制矩形
            rect = patches.Rectangle(
                (x1, y1), x2 - x1, y2 - y1,
                linewidth=2, edgecolor=color, facecolor=color, alpha=0.3
            )
            ax1.add_patch(rect)
            ax1.add_patch(patches.Rectangle(
                (x1, y1), x2 - x1, y2 - y1,
                linewidth=2, edgecolor=color, facecolor='none'
            ))

            # 显示行标签
            label_text = str(label) if label != -1 else '?'
            ax1.text((x1 + x2) / 2, (y1 + y2) / 2, label_text,
                     color='black', fontsize=8, ha='center', va='center',
                     weight='bold', bbox=dict(boxstyle='round', facecolor='white', alpha=0.7))

    # 设置坐标轴范围（留边距）
    margin_x = img_width * 0.02
    margin_y = img_height * 0.02
    ax1.set_xlim(-margin_x, img_width + margin_x)
    ax1.set_ylim(img_height + margin_y, -margin_y)  # 反转Y轴
    ax1.set_xlabel('X 坐标 (像素)')
    ax1.set_ylabel('Y 坐标 (像素)')
    ax1.set_title(f'检测框按行着色（共 {len(set([l for l in row_labels if l != -1]))} 行）')
    ax1.grid(True, alpha=0.3)

    # ===== 右图：显示Y轴投影直方图 =====
    ax2 = axes[1]

    if len(boxes) > 0:
        # 提取所有中心Y坐标
        centers_y = []
        for box in boxes:
            y = [i[1] for i in box]
            centers_y.append((max(y) + min(y)) / 2.0)

        # 绘制水平直方图
        n_bins = min(50, max(10, len(boxes) // 2))
        ax2.hist(centers_y, bins=n_bins, orientation='horizontal',
                 color='steelblue', alpha=0.7, edgecolor='black', linewidth=0.5)

        # 标记聚类中心（行中心）
        for center_y in row_centers:
            ax2.axhline(y=center_y, color='red', linestyle='--', linewidth=2,
                        label=f'行中心: {center_y:.1f}')

        # 标记每行聚类中心（仅显示一次图例）
        if row_centers:
            ax2.axhline(y=row_centers[0], color='red', linestyle='--', linewidth=2,
                        label='聚类中心')

        # 设置坐标轴
        ax2.set_xlabel('岩芯数量')
        ax2.set_ylabel('Y 坐标 (像素)')
        ax2.set_title('Y轴投影分布（簇对应行）')
        ax2.set_ylim(img_height + margin_y, -margin_y)  # 与左图一致
        ax2.grid(True, alpha=0.3)

        # 添加图例
        ax2.legend()

    # 设置主标题
    fig.suptitle(title, fontsize=16, weight='bold')
    plt.tight_layout()
    plt.savefig('./1.jpg', dpi=300, bbox_inches='tight')
    plt.show()

    return fig, axes

def assign_tags_to_rows(boxes, row_labels, row_count):
    """
    将岩芯按行分组

    参数:
        boxes: 检测框列表
        row_labels: 每行标签
        row_count: 行数

    返回:
        row_groups: 按行分组的检测框索引列表
    """
    row_groups = [[] for _ in range(row_count)]
    for i, label in enumerate(row_labels):
        if label != -1 and label < row_count:
            row_groups[label].append(i)
    return row_groups

def caculate_eps_ratio(image_path, boxes):
    # 获取图像尺寸
    with Image.open(image_path) as img:
        w, h = img.size
    # 计算岩芯平均高度
    box_heights = []
    for box in boxes:
        y = [i[1] for i in box]
        box_height = max(y) - min(y)
        box_heights.append(box_height)
    ava_height = sum(box_heights) / len(box_heights)

    return ava_height / h / 2


# ===========================================================================
# 多边形 X 方向裁剪（Sutherland-Hodgman 对竖直线的裁剪）
# ===========================================================================

def _clip_against_vertical(polygon, x_boundary, keep_geq):
    """
    Sutherland-Hodgman: 将多边形裁剪到竖直线 x = x_boundary 的一侧。

    参数:
        polygon: 多边形顶点 [[x, y], ...]
        x_boundary: 竖直裁剪线 X 坐标
        keep_geq: True → 保留 x >= x_boundary 的部分；False → 保留 x <= x_boundary

    返回:
        裁剪后的顶点列表
    """
    if not polygon:
        return []

    result = []
    n = len(polygon)

    for i in range(n):
        curr = polygon[i]
        prev = polygon[i - 1]  # i=0 时回溯到最后一个顶点

        if keep_geq:
            curr_in = curr[0] >= x_boundary
            prev_in = prev[0] >= x_boundary
        else:
            curr_in = curr[0] <= x_boundary
            prev_in = prev[0] <= x_boundary

        if curr_in:
            if not prev_in:
                # 从外部进入：先输出交点
                dx = curr[0] - prev[0]
                t = (x_boundary - prev[0]) / dx if dx != 0 else 0.0
                iy = prev[1] + t * (curr[1] - prev[1])
                result.append([x_boundary, iy])
            result.append([curr[0], curr[1]])
        else:
            if prev_in:
                # 从内部离开：输出交点
                dx = curr[0] - prev[0]
                t = (x_boundary - prev[0]) / dx if dx != 0 else 0.0
                iy = prev[1] + t * (curr[1] - prev[1])
                result.append([x_boundary, iy])

    return result


def _clip_polygon_to_x_range(polygon, x_min, x_max):
    """
    将多边形裁剪到竖直条带 [x_min, x_max] 内。

    参数:
        polygon: 多边形顶点 [[x, y], ...]
        x_min: 条带左边界
        x_max: 条带右边界

    返回:
        裁剪后的顶点列表（可能为空）
    """
    if not polygon or len(polygon) < 2:
        return []
    clipped = _clip_against_vertical(polygon, x_min, keep_geq=True)
    clipped = _clip_against_vertical(clipped, x_max, keep_geq=False)
    return clipped


# ===========================================================================
# 行中心线：分段局部 Y → 随 X 变化的行几何（逻辑闭合用）
# ===========================================================================

def _polygon_center(poly):
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return (min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0


def _tag_center(tag):
    """岩芯牌中心。兼容 [x, y] 与 [x1, y1, x2, y2]。"""
    if tag is None or len(tag) < 2:
        return None
    if len(tag) >= 4:
        return ((float(tag[0]) + float(tag[2])) / 2.0,
                (float(tag[1]) + float(tag[3])) / 2.0)
    return (float(tag[0]), float(tag[1]))


def _median_positive(values):
    vals = [float(v) for v in values if v is not None and v > 0]
    if not vals:
        return None
    return float(np.median(vals))


def _typical_row_pitch(segment_results, preferred_count=None):
    """从各段簇中心的相邻差估计行距。优先用行数=preferred_count 的段。"""
    pools = []
    if preferred_count is not None:
        for seg in segment_results:
            if seg['row_count'] == preferred_count and len(seg['centers']) >= 2:
                c = seg['centers']
                pools.extend(c[i + 1] - c[i] for i in range(len(c) - 1))
    if not pools:
        for seg in segment_results:
            c = seg['centers']
            if len(c) >= 2:
                pools.extend(c[i + 1] - c[i] for i in range(len(c) - 1))
    return _median_positive(pools)


def _greedy_match_centers(centers, ref_ys, max_dist):
    """
    将本段簇中心匹配到已有行。相邻竖带弯曲量应小于半个行距，
    用 Y 最近且一对一的贪心即可，不必上全局 KMeans。
    """
    pairs = []
    for ci, cy in enumerate(centers):
        for ri, ry in enumerate(ref_ys):
            if ry is None:
                continue
            d = abs(float(cy) - float(ry))
            if d <= max_dist:
                pairs.append((d, ci, ri))
    pairs.sort()
    used_c, used_r = set(), set()
    assignment = {}
    for d, ci, ri in pairs:
        if ci in used_c or ri in used_r:
            continue
        used_c.add(ci)
        used_r.add(ri)
        assignment[ri] = float(centers[ci])
    return assignment


def _eval_centerline(samples, x, ref_eval=None):
    """
    在横坐标 x 处读取一行的 Y。
    样本覆盖范围内线性插值；范围外沿参考行平行延拓（各行共享同一弯曲），
    禁止用水平线外推——那正是全局一维 Y 的错误模型。
    """
    if not samples:
        return None if ref_eval is None else ref_eval(x)
    pts = sorted(samples, key=lambda p: p[0])
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    x = float(x)
    if len(pts) == 1:
        if ref_eval is None:
            return float(ys[0])
        return ref_eval(x) + (ys[0] - ref_eval(xs[0]))
    if xs[0] <= x <= xs[-1]:
        return float(np.interp(x, xs, ys))
    if ref_eval is None:
        if x < xs[0]:
            dx = xs[1] - xs[0]
            slope = (ys[1] - ys[0]) / dx if dx != 0 else 0.0
            return ys[0] + slope * (x - xs[0])
        dx = xs[-1] - xs[-2]
        slope = (ys[-1] - ys[-2]) / dx if dx != 0 else 0.0
        return ys[-1] + slope * (x - xs[-1])
    anchor_x = xs[0] if x < xs[0] else xs[-1]
    anchor_y = ys[0] if x < xs[0] else ys[-1]
    return ref_eval(x) + (anchor_y - ref_eval(anchor_x))


def _make_centerline_evaluator(centerlines):
    """centerlines: list[list[(x, y)]]。样本最多的一行作为弯曲参考。"""
    if not centerlines:
        return lambda r, x: None
    ref_idx = max(
        range(len(centerlines)),
        key=lambda i: (
            len(centerlines[i]),
            (max(p[0] for p in centerlines[i]) - min(p[0] for p in centerlines[i]))
            if centerlines[i] else 0,
        ),
    )
    if not centerlines[ref_idx]:
        ref_idx = next((i for i, s in enumerate(centerlines) if s), 0)

    def eval_ref(x):
        return _eval_centerline(centerlines[ref_idx], x, ref_eval=None)

    def eval_row(r, x):
        if r < 0 or r >= len(centerlines):
            return None
        if r == ref_idx:
            return eval_ref(x)
        return _eval_centerline(centerlines[r], x, ref_eval=eval_ref)

    return eval_row


def _build_row_centerlines(segment_results, final_N, img_width, n_segments, pitch, eps_px):
    """
    从各段簇中心构造 N 条随 X 变化的行中心线。
    种子段（行数=final_N 的中位段）按 Y 排序认行；其余段按与邻段的 Y 连续性匹配，
    不把“第 k 个簇”无条件当成全箱第 k 行。
    """
    centerlines = [[] for _ in range(final_N)]
    if not segment_results or final_N <= 0:
        return centerlines

    max_dist = 0.45 * pitch if (pitch is not None and pitch > 0) else max(3.0 * eps_px, 1.0)
    by_idx = {seg['seg_idx']: seg for seg in segment_results}
    good_ids = sorted(i for i, seg in by_idx.items() if seg['row_count'] == final_N)
    if good_ids:
        seed_id = good_ids[len(good_ids) // 2]
    else:
        seed_id = sorted(by_idx.keys())[len(by_idx) // 2]

    seed = by_idx[seed_id]
    seed_centers = list(seed['centers'])
    if seed['row_count'] == final_N and len(seed_centers) == final_N:
        seed_assign = {r: float(seed_centers[r]) for r in range(final_N)}
    else:
        if len(seed_centers) >= 2:
            grid0, grid1 = float(seed_centers[0]), float(seed_centers[-1])
        else:
            grid0, grid1 = 0.0, float(seed_centers[0]) if seed_centers else 0.0
        grid = list(np.linspace(grid0, grid1, final_N)) if final_N > 1 else [grid0]
        seed_assign = _greedy_match_centers(seed_centers, grid, max_dist=1e9)
        for r, cy in enumerate(seed_centers):
            if r < final_N and r not in seed_assign:
                seed_assign[r] = float(cy)

    x_mid = 0.5 * (seed['x_start'] + seed['x_end'])
    current = [seed_assign.get(r) for r in range(final_N)]
    for r, y in seed_assign.items():
        if 0 <= r < final_N:
            centerlines[r].append((x_mid, y))

    def consume(seg, ref_ys):
        x = 0.5 * (seg['x_start'] + seg['x_end'])
        matched = _greedy_match_centers(seg['centers'], ref_ys, max_dist)
        if seg['row_count'] == final_N and len(matched) < final_N:
            used_y = set(matched.values())
            unused_rows = [r for r in range(final_N) if r not in matched]
            unused_c = [float(c) for c in seg['centers'] if float(c) not in used_y]
            unused_c.sort()
            for r, cy in zip(unused_rows, unused_c):
                ref = ref_ys[r] if ref_ys[r] is not None else cy
                if abs(cy - ref) <= max_dist * 1.5:
                    matched[r] = cy
        for r, y in matched.items():
            centerlines[r].append((x, y))
        new_ref = list(ref_ys)
        for r, y in matched.items():
            new_ref[r] = y
        return new_ref

    ordered_ids = sorted(by_idx.keys())
    ref_ys = list(current)
    for sid in ordered_ids:
        if sid <= seed_id:
            continue
        ref_ys = consume(by_idx[sid], ref_ys)
    ref_ys = list(current)
    for sid in reversed(ordered_ids):
        if sid >= seed_id:
            continue
        ref_ys = consume(by_idx[sid], ref_ys)

    for r in range(final_N):
        centerlines[r].sort(key=lambda p: p[0])

    populated = [r for r in range(final_N) if centerlines[r]]
    if populated and pitch:
        src = min(populated, key=lambda q: -len(centerlines[q]))
        for r in range(final_N):
            if centerlines[r]:
                continue
            delta = (r - src) * pitch
            centerlines[r] = [(x, y + delta) for x, y in centerlines[src]]
    return centerlines


def _assign_boxes_by_centerlines(boxes, centerlines):
    """按多边形中心 X 处各行中心线的 Y，把整框贴到最近一行。"""
    n = len(centerlines)
    if n == 0:
        return 0, [], []
    eval_row = _make_centerline_evaluator(centerlines)
    labels = []
    for box in boxes:
        cx, cy = _polygon_center(box)
        best_r, best_d = 0, float('inf')
        for r in range(n):
            yr = eval_row(r, cx)
            if yr is None:
                continue
            d = abs(cy - yr)
            if d < best_d:
                best_d = d
                best_r = r
        labels.append(best_r)
    means = []
    for r in range(n):
        if centerlines[r]:
            means.append(float(np.mean([p[1] for p in centerlines[r]])))
        else:
            ys = [_polygon_center(boxes[i])[1] for i, lab in enumerate(labels) if lab == r]
            means.append(float(np.mean(ys)) if ys else 0.0)
    return n, labels, means


def _contiguous_end_block(plus1_set, n_segments, from_start):
    """从箱首或箱尾数连续的 N+1 段个数。"""
    if from_start:
        k = 0
        while k < n_segments and k in plus1_set:
            k += 1
        return k
    k = 0
    s = n_segments - 1
    while s >= 0 and s in plus1_set:
        k += 1
        s -= 1
    return k


def _local_extra_gap(seg):
    """同一竖带内最末两簇的 Y 间隔（弯曲不变量）。"""
    c = seg.get('centers') or []
    if len(c) < 2:
        return None
    return float(c[-1] - c[-2])


# ===========================================================================
# 分段 DBSCAN 投票 + 行中心线分配
# ===========================================================================

def detect_rows_by_segmented_projection(boxes, img_width, img_height, image_path=None):
    """
    分段 DBSCAN 行检测（主函数）。

    算法流程：
      1. 沿 X 轴将图像分成 10 个竖条
      2. 对每个多边形按条带边界做 X 方向裁剪（非简单按中心分配）
      3. 每条独立做 DBSCAN 聚类 → 得到该条候选行数和簇中心
      4. 众数投票确定基准行数 N；箱首或箱尾连续出现 N+1 则候选末行不满
      5. 用**同一竖带内**最末两簇间距与行距比较，决定是否撤销 +1
      6. 按邻段 Y 连续性把各段簇中心织成 N 条随 X 变化的行中心线
      7. 每个多边形按其自身 X 处与中心线的 Y 距离归属，不再做全图一维 KMeans

    与 detect_rows_from_boxes（全局 DBSCAN）相比，此方法：
      - 通过分段解决了弯曲隔板导致全局 Y 聚类行数误判的问题
      - 通过投票机制滤除单段噪声（破碎岩芯、稀疏段）
      - 通过裁剪多边形（而非按中心分配）保证段内 Y 范围准确
      - 通过行中心线把“弯曲”从数行贯彻到行归属，避免数行与贴标签使用两套几何

    参数:
        boxes: 多边形顶点列表 [[[x, y], ...], ...]
        img_width: 图像宽度（像素）
        img_height: 图像高度（像素）
        image_path: str | None — 透视变换图片路径，提供时自动保存分段检测可视化图

    返回:
        row_count: int
        row_labels: list[int]
        cluster_centers: list[float]    — 各行中心线的平均 Y（兼容旧接口）
        centerlines: list[list[(x, y)]] — 各行随 X 变化的中心线样本
    """
    if len(boxes) == 0:
        return 0, [], [], []

    # ---- 1. 确定分段数 ----
    box_widths = []
    box_heights = []
    for box in boxes:
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        box_widths.append(max(xs) - min(xs))
        box_heights.append(max(ys) - min(ys))

    avg_width = float(np.mean(box_widths)) if box_widths else img_width / 5.0
    median_height = float(np.median(box_heights)) if box_heights else 30.0
    avg_height = float(np.mean(box_heights)) if box_heights else 30.0

    # eps_ratio 基准值（与 caculate_eps_ratio 等价：ava_height / img_height / 2，
    # 但直接用传入的 img_height 计算，不依赖 image_path，避免 image_path=None 时崩溃）
    eps_ratio_base = median_height / img_height / 2.0

    n_segments = 10
    seg_width = img_width / n_segments

    print(f"\n{'='*70}")
    print(f"[分段投影] 图像 {img_width}x{img_height}, 多边形数={len(boxes)}, "
          f"平均宽度={avg_width:.1f}, 中位高度={median_height:.1f}")
    print(f"[分段投影] 分 {n_segments} 段, 每段宽度={seg_width:.1f}")
    print(f"{'='*70}")

    # ---- 2. 逐段处理：每段 DBSCAN 聚类检测行数 ----
    segment_results = []  # [{'row_count': int, 'centers': [float, ...]}]
    segment_debug = []   # 可视化用：每段的裁剪多边形 + DBSCAN标签

    for s in range(n_segments):
        x_start = s * seg_width
        x_end = (s + 1) * seg_width if s < n_segments - 1 else float(img_width) + 1.0

        # 对每个多边形进行 X 方向裁剪
        clipped_boxes = []
        for box in boxes:
            clipped = _clip_polygon_to_x_range(box, x_start, x_end)
            if not clipped or len(clipped) < 2:
                continue
            ys = [p[1] for p in clipped]
            if max(ys) - min(ys) < 1.0:
                continue
            clipped_boxes.append(clipped)

        if len(clipped_boxes) < 2:
            print(f"  段{s} X[{x_start:.0f},{x_end:.0f}]: 有效多边形={len(clipped_boxes)} < 2, 跳过")
            segment_debug.append({
                'seg_idx': s, 'x_start': x_start, 'x_end': x_end,
                'clipped_boxes': [], 'dblabels': [], 'row_count': 0,
            })
            continue

        # 对裁剪后的多边形做 DBSCAN 聚类
        # eps_ratio 用预计算的 eps_ratio_base * 0.8（与 caculate_eps_ratio * 0.8 等价，但不依赖 image_path）
        rc_s, labels_s, centers_s = detect_rows_from_boxes(
            clipped_boxes, eps_ratio=eps_ratio_base * 0.8, min_samples=1,
            img_height=img_height
        )

        if rc_s == 0:
            print(f"  段{s} X[{x_start:.0f},{x_end:.0f}]: DBSCAN 未检出行, 跳过")
            segment_debug.append({
                'seg_idx': s, 'x_start': x_start, 'x_end': x_end,
                'clipped_boxes': clipped_boxes, 'dblabels': labels_s, 'row_count': 0,
            })
            continue

        print(f"  段{s} X[{x_start:.0f},{x_end:.0f}]: "
              f"多边形={len(clipped_boxes)}, 检测行数={rc_s}, "
              f"簇中心Y={[round(float(c), 1) for c in centers_s]}")

        segment_results.append({
            'seg_idx': s,
            'row_count': rc_s,
            'centers': centers_s,
            'x_start': x_start,
            'x_end': x_end,
        })
        segment_debug.append({
            'seg_idx': s, 'x_start': x_start, 'x_end': x_end,
            'clipped_boxes': clipped_boxes, 'dblabels': labels_s, 'row_count': rc_s,
        })

    # ---- 3. 投票确定行数 + 箱首/箱尾连续不满末行 ----
    if not segment_results:
        print(f"[分段投影] 无有效分段结果, 回退到全局 DBSCAN")
        rc, lab, cen = detect_rows_from_boxes(
            boxes, eps_ratio=eps_ratio_base * 0.8, min_samples=1,
            img_height=img_height
        )
        fallback_lines = [[(0.0, float(c)), (float(img_width), float(c))] for c in cen]
        return rc, lab, cen, fallback_lines

    row_counts = [r['row_count'] for r in segment_results]
    count_votes = Counter(row_counts)
    max_votes = count_votes.most_common(1)[0][1]
    tied = sorted([rc for rc, v in count_votes.items() if v == max_votes])
    voted_N = tied[0]

    n_plus1 = voted_N + 1
    plus1_segs = [seg['seg_idx'] for seg in segment_results if seg['row_count'] == n_plus1]
    plus1_set = set(plus1_segs)
    plus1_in_first_two = [i for i in plus1_segs if i < 2]
    plus1_in_later = [i for i in plus1_segs if i >= 2]

    prefix_plus = _contiguous_end_block(plus1_set, n_segments, from_start=True)
    suffix_plus = _contiguous_end_block(plus1_set, n_segments, from_start=False)
    # 中间孤立的 N+1 视为噪声；只有贴着箱首或箱尾的连续块才是不满末行
    has_partial_last = prefix_plus >= 1 or suffix_plus >= 1
    partial_side = (
        'prefix' if prefix_plus and not suffix_plus else
        'suffix' if suffix_plus and not prefix_plus else
        'both' if prefix_plus and suffix_plus else
        'none'
    )
    final_N = voted_N + 1 if has_partial_last else voted_N

    print(f"\n{'─'*70}")
    print(f"[分段投影] 各有效段行数: {row_counts}")
    print(f"[分段投影] 投票分布: {dict(count_votes)}")
    print(f"[分段投影] 众数基准行数 N={voted_N}")
    if plus1_segs:
        print(f"[分段投影] 众数+1={n_plus1} 出现在段: {plus1_segs} "
              f"(箱首连续{prefix_plus}段, 箱尾连续{suffix_plus}段, 侧={partial_side})")
        if has_partial_last:
            print(f"  → 连续端块判定为末行不满，暂定候选行数={final_N}")
        else:
            print(f"  → N+1 不在箱首/箱尾连续块，视为噪声，候选行数={final_N}")
    else:
        print(f"[分段投影] 未检测到众数+1，候选行数={final_N}")
    print(f"{'─'*70}")

    # ---- 4. 末行误判校验：同一竖带内的簇间距 vs 行距（弯曲不变量）----
    eps_ratio_check = eps_ratio_base * 0.8
    eps_px = eps_ratio_check * float(img_height)
    pitch = _typical_row_pitch(segment_results, preferred_count=voted_N)
    plus1_revoked = False
    extra_gap = None
    gap_threshold = None
    check_seg = None

    if has_partial_last and final_N >= 2:
        by_idx = {seg['seg_idx']: seg for seg in segment_results}
        if prefix_plus >= 1 and 0 in by_idx:
            check_seg = by_idx[0]
        elif suffix_plus >= 1:
            last_id = max(plus1_set)
            check_seg = by_idx.get(last_id)
        extra_gap = _local_extra_gap(check_seg) if check_seg is not None else None
        eps_cut = 2.0 * eps_px
        pitch_cut = 0.35 * pitch if pitch is not None else None
        # 新行必须在同段内拉开到行距量级：同时大于 2*eps 与 0.35*行距
        gap_threshold = eps_cut if pitch_cut is None else max(eps_cut, pitch_cut)

        print(f"[分段投影] 末行误判校验(同段间距): "
              f"检查段={check_seg['seg_idx'] if check_seg else None}, "
              f"末两簇间距={None if extra_gap is None else round(extra_gap, 1)}, "
              f"行距估计={None if pitch is None else round(pitch, 1)}, "
              f"2*eps={eps_cut:.1f}px, 阈值={None if gap_threshold is None else round(gap_threshold, 1)}")

        if extra_gap is None or gap_threshold is None:
            print("  → 间距数据不足，跳过校验，保留 N+1")
        elif extra_gap < gap_threshold:
            print(f"  → 同段末两簇间距小于阈值，判定为拆行误检，最终行数改为{voted_N}")
            final_N = voted_N
            has_partial_last = False
            plus1_revoked = True
        else:
            print(f"  → 同段末两簇间距达到行距量级，保留 N+1，最终行数={final_N}")

        # 可视化：同一检查段的最末两簇（不再拿不同列的绝对 Y 相比）
        try:
            if check_seg is not None and extra_gap is not None and image_path:
                dbg = None
                for d in segment_debug:
                    if d['seg_idx'] == check_seg['seg_idx']:
                        dbg = d
                        break
                last_polys, last_labels = [], []
                prev_polys, prev_labels = [], []
                if dbg is not None:
                    top_label = check_seg['row_count'] - 1
                    prev_label = check_seg['row_count'] - 2
                    for poly, lab in zip(dbg.get('clipped_boxes', []), dbg.get('dblabels', [])):
                        if lab == top_label:
                            last_polys.append(poly)
                            last_labels.append(f"S{dbg['seg_idx']}-L{lab}")
                        elif lab == prev_label:
                            prev_polys.append(poly)
                            prev_labels.append(f"S{dbg['seg_idx']}-L{lab}")
                c = check_seg['centers']
                mean_last = float(c[-1])
                mean_prev = float(c[-2]) if len(c) >= 2 else mean_last
                _visualize_plus1_revoke(
                    image_path=image_path,
                    img_width=img_width,
                    img_height=img_height,
                    seg0_last_polys=last_polys,
                    seg0_last_labels=last_labels,
                    later_last_polys=prev_polys,
                    later_last_labels=prev_labels,
                    mean_y_last=mean_last,
                    mean_y_last_ref=mean_prev,
                    diff_ratio=extra_gap / float(img_height),
                    threshold=(gap_threshold or 0) / float(img_height),
                    eps_ratio_check=eps_ratio_check,
                    plus1_revoked=plus1_revoked,
                    voted_N=voted_N,
                    final_N=final_N,
                    reference_segment_indices=[check_seg['seg_idx']],
                )
        except Exception as e:
            print(f"[分段投影] 误判校验可视化失败（不影响计算）: {e}")

    print(f"[分段投影] 误判校验完成，最终确定行数={final_N}；现在按行中心线归属（不再使用全图 KMeans）")

    # ---- 5. 邻段连续性织成行中心线，按中心线分配 ----
    if pitch is None:
        pitch = _typical_row_pitch(segment_results, preferred_count=final_N)
    centerlines = _build_row_centerlines(
        segment_results, final_N, img_width, n_segments,
        pitch=pitch, eps_px=eps_px,
    )
    row_count, row_labels, cluster_centers = _assign_boxes_by_centerlines(boxes, centerlines)
    print(f"[分段投影] 行中心线样本数={[len(s) for s in centerlines]}")
    print(f"[分段投影] 中心线归属完成, 行数={row_count}, "
          f"平均Y={[round(float(c), 1) for c in cluster_centers]}")
    print(f"{'='*70}\n")

    # ---- 6. 保存分段检测可视化图 ----
    if image_path:
        try:
            _save_segment_debug_image(
                image_path=image_path,
                img_width=img_width,
                img_height=img_height,
                n_segments=n_segments,
                seg_width=seg_width,
                segment_debug=segment_debug,
                row_counts_list=row_counts,
                count_votes=dict(count_votes),
                voted_N=voted_N,
                plus1_segs=plus1_segs,
                plus1_in_first_two=plus1_in_first_two,
                plus1_in_later=plus1_in_later,
                final_N=final_N,
                has_partial_last=has_partial_last,
                plus1_revoked=plus1_revoked,
                cluster_centers=cluster_centers,
                row_labels=row_labels,
                boxes=boxes,
                centerlines=centerlines,
            )
        except Exception as e:
            print(f"[分段投影] 可视化图保存失败（不影响计算结果）: {e}")

    return row_count, row_labels, cluster_centers, centerlines


# ===========================================================================
# 分段检测可视化图
# ===========================================================================

# DBSCAN 簇颜色调色板（每段内按簇编号循环）
_SEG_CLUSTER_COLORS = [
    (255, 100, 100),    # 红
    (100, 100, 255),    # 蓝
    (100, 200, 100),    # 绿
    (255, 200, 0),      # 金
    (200, 100, 200),    # 紫
    (0, 200, 200),      # 青
    (255, 150, 0),      # 橙
    (150, 200, 255),    # 浅蓝
    (255, 100, 200),    # 粉
    (200, 200, 100),    # 橄榄
    (100, 255, 150),    # 薄荷
    (180, 180, 180),    # 灰
]


def _get_debug_font(size: int = 20):
    """获取支持中文的字体"""
    for fp in [
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simsun.ttc",
    ]:
        try:
            return ImageFont.truetype(fp, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _save_segment_debug_image(
    image_path, img_width, img_height, n_segments, seg_width,
    segment_debug, row_counts_list, count_votes, voted_N,
    plus1_segs, plus1_in_first_two, plus1_in_later,
    final_N, has_partial_last, plus1_revoked, cluster_centers, row_labels, boxes,
    centerlines=None,
):
    """
    生成分段检测可视化图并保存到 4_table_info_image 文件夹。

    图中包含：
    - 原始透视校正图片作为背景
    - 10 条竖直虚线标记分段边界（顶部标段号）
    - 每段内裁剪后的多边形按 DBSCAN 簇着色填充
    - 红色折线标记随 X 变化的行中心线（不再画全图水平 KMeans 线）
    - 顶部白色信息栏：各段行数、投票分布、最终行数
    """
    # 推断输出目录
    persp_dir = os.path.dirname(image_path)
    session_dir = os.path.dirname(persp_dir)
    output_dir = os.path.join(session_dir, "4_table_info_image")
    os.makedirs(output_dir, exist_ok=True)
    image_basename = os.path.splitext(os.path.basename(image_path))[0]
    out_path = os.path.join(output_dir, f"{image_basename}_segment_debug.jpg")

    # ---- 加载原始图片 ----
    orig = Image.open(image_path).convert("RGB")
    ow, oh = orig.size

    # ---- 顶部信息栏高度 ----
    font_sm = _get_debug_font(16)
    font_md = _get_debug_font(20)
    font_lg = _get_debug_font(26)

    # 计算信息栏所需高度
    banner_h = 120

    # ---- 创建画布（图片 + 顶部信息栏）----
    canvas = Image.new("RGB", (ow, oh + banner_h), (255, 255, 255))
    canvas.paste(orig, (0, banner_h))
    draw = ImageDraw.Draw(canvas)

    # ================================================================
    # 1. 顶部信息栏文字
    # ================================================================
    y_cursor = 8

    # 第一行：各段行数
    seg_row_str = "  ".join(
        f"S{d['seg_idx']}:{d['row_count']}" for d in segment_debug
    )
    draw.text((10, y_cursor), f"各段行数: {seg_row_str}",
              fill=(0, 0, 0), font=font_sm)
    y_cursor += 22

    # 第二行：投票分布 + 众数+1判断
    votes_str = "  ".join(f"{k}行:{v}票" for k, v in sorted(count_votes.items()))
    draw.text((10, y_cursor), f"投票: {votes_str}  →  众数={voted_N}",
              fill=(0, 0, 0), font=font_sm)
    y_cursor += 22

    # 第三行：众数+1判断结果
    if plus1_segs:
        if plus1_revoked:
            judge_str = f"众数+1={voted_N+1} 第1段检到 → 末行误判校验撤销+1 → 用众数"
            judge_fill = (200, 120, 0)  # 橙色：撤销
        elif has_partial_last:
            later_str = f"(后面段{plus1_in_later}误检仍+1)" if plus1_in_later else "(仅前2段)"
            judge_str = f"众数+1={voted_N+1} 第1段检到 → 末行不满 → +1 {later_str}"
            judge_fill = (200, 0, 0)
        else:
            judge_str = f"众数+1={voted_N+1} 第1段未检到(仅段{plus1_in_first_two}) → 不+1"
            judge_fill = (0, 100, 0)
        draw.text((10, y_cursor), f"{judge_str}  →  最终行数={final_N}",
                  fill=judge_fill, font=font_sm)
    else:
        draw.text((10, y_cursor), f"无众数+1  →  最终行数={final_N}",
                  fill=(0, 0, 0), font=font_sm)
    y_cursor += 22

    # 第四行：最终簇中心 Y
    centers_str = ", ".join(
        f"行{i+1}:Y={c:.0f}" for i, c in enumerate(cluster_centers)
    )
    draw.text((10, y_cursor), f"KMeans簇中心: {centers_str}",
              fill=(200, 0, 0), font=font_sm)
    y_cursor += 22

    # 第五行：图例
    legend_x = 10
    draw.text((legend_x, y_cursor), "图例:", fill=(80, 80, 80), font=font_sm)
    legend_x += 50
    # 分段边界
    draw.line([(legend_x, y_cursor + 8), (legend_x + 30, y_cursor + 8)],
              fill=(180, 180, 180), width=2)
    draw.text((legend_x + 35, y_cursor), "分段边界",
              fill=(80, 80, 80), font=font_sm)
    legend_x += 130
    # KMeans中心
    draw.line([(legend_x, y_cursor + 8), (legend_x + 30, y_cursor + 8)],
              fill=(255, 0, 0), width=3)
    draw.text((legend_x + 35, y_cursor), "KMeans行中心",
              fill=(80, 80, 80), font=font_sm)
    legend_x += 160
    # DBSCAN簇着色
    draw.rectangle([legend_x, y_cursor + 2, legend_x + 16, y_cursor + 16],
                   fill=(100, 200, 100))
    draw.text((legend_x + 20, y_cursor), "DBSCAN簇着色",
              fill=(80, 80, 80), font=font_sm)

    # ================================================================
    # 2. 分段边界竖线 + 段号
    # ================================================================
    for s in range(n_segments):
        x = s * seg_width
        # 虚线
        for y in range(banner_h, banner_h + oh, 12):
            draw.line([(x, y), (x, min(y + 6, banner_h + oh))],
                      fill=(200, 200, 200), width=2)
        # 段号
        seg_label = f"S{s}"
        bbox = draw.textbbox((0, 0), seg_label, font=font_sm)
        tw = bbox[2] - bbox[0]
        draw.rectangle(
            [x + 2, banner_h + 2, x + tw + 12, banner_h + 20],
            fill=(255, 255, 200), outline=(180, 180, 100),
        )
        draw.text((x + 6, banner_h + 3), seg_label,
                  fill=(100, 100, 0), font=font_sm)

    # 最后一条边界
    draw.line([(img_width, banner_h), (img_width, banner_h + oh)],
              fill=(200, 200, 200), width=2)

    # ================================================================
    # 3. 每段内裁剪多边形按 DBSCAN 簇着色
    # ================================================================
    for seg in segment_debug:
        clipped_boxes = seg.get('clipped_boxes', [])
        dblabels = seg.get('dblabels', [])
        for i, poly in enumerate(clipped_boxes):
            if i >= len(dblabels):
                break
            lbl = dblabels[i]
            if lbl == -1:
                color = (128, 128, 128)  # 噪声灰色
            else:
                color = _SEG_CLUSTER_COLORS[lbl % len(_SEG_CLUSTER_COLORS)]
            # 浅色填充 = 原色与白色 3:7 混合
            fill_color = tuple(int(c * 0.3 + 255 * 0.7) for c in color)
            pts = [(float(p[0]), float(p[1]) + banner_h) for p in poly]
            if len(pts) >= 3:
                draw.polygon(pts, fill=fill_color, outline=color, width=2)

    # ================================================================
    # 4. 行中心线（随 X 变化；无中心线时退回平均 Y 水平线）
    # ================================================================
    eval_row = _make_centerline_evaluator(centerlines) if centerlines else None
    for i, cy in enumerate(cluster_centers):
        if eval_row is not None:
            xs_line = list(range(0, max(int(img_width), 1) + 1, max(int(img_width) // 80, 8)))
            if not xs_line or xs_line[-1] != int(img_width):
                xs_line.append(int(img_width))
            pts = []
            for x in xs_line:
                yr = eval_row(i, x)
                if yr is None:
                    continue
                pts.append((float(x), float(yr) + banner_h))
            if len(pts) >= 2:
                draw.line(pts, fill=(255, 0, 0), width=3)
            y_label = pts[0][1] if pts else cy + banner_h
        else:
            y_label = cy + banner_h
            draw.line([(0, y_label), (img_width, y_label)], fill=(255, 0, 0), width=3)
        label = f"行{i+1}"
        bbox = draw.textbbox((0, 0), label, font=font_sm)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        draw.rectangle(
            [4, y_label - th - 4, tw + 14, y_label + 2],
            fill=(255, 255, 255), outline=(255, 0, 0),
        )
        draw.text((8, y_label - th - 3), label, fill=(255, 0, 0), font=font_sm)

    # ================================================================
    # 5. 行中心线归属标签（标注在每个多边形中心）
    # ================================================================
    for i, box in enumerate(boxes):
        if i >= len(row_labels):
            continue
        lbl = row_labels[i]
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        cx = (min(xs) + max(xs)) / 2.0
        cy = (min(ys) + max(ys)) / 2.0
        color = _SEG_CLUSTER_COLORS[lbl % len(_SEG_CLUSTER_COLORS)]
        label = f"R{lbl}"
        bbox = draw.textbbox((0, 0), label, font=font_sm)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        draw.rectangle(
            [cx - tw/2 - 2, cy + banner_h - th/2 - 1,
             cx + tw/2 + 2, cy + banner_h + th/2 + 1],
            fill=(255, 255, 255, 200),
        )
        draw.text((cx - tw/2, cy + banner_h - th/2 - 1), label,
                  fill=color, font=font_sm)

    # ================================================================
    # 保存
    # ================================================================
    canvas.save(out_path, "JPEG", quality=90)
    print(f"[分段投影] 可视化图已保存: {out_path}")


# ===========================================================================
# 末行误判校验可视化
# ===========================================================================

def _visualize_plus1_revoke(
    image_path, img_width, img_height,
    seg0_last_polys, seg0_last_labels,
    later_last_polys, later_last_labels,
    mean_y_last, mean_y_last_ref,
    diff_ratio, threshold, eps_ratio_check,
    plus1_revoked, voted_N, final_N,
    reference_segment_indices,
):
    """
    生成末行误判校验的可视化图，以原始图片为底图标记：
    - 段0第 N+1 行（label=voted_N）的所有裁剪多边形 → 蓝色填充；
    - 段1~4中行数恰为 N 的参考段之第 N 行（label=voted_N-1） → 绿色填充；
    - 两组的总体平均 Y 线（红/绿虚线）；
    - 每个多边形的中心点 Y 标记（小圆点 + 数值标注）；
    - 信息栏显示参考段、判定参数与结果。
    """
    if image_path is None:
        return
    # 输出目录
    persp_dir = os.path.dirname(image_path)
    session_dir = os.path.dirname(persp_dir)
    output_dir = os.path.join(session_dir, "4_table_info_image")
    os.makedirs(output_dir, exist_ok=True)
    image_basename = os.path.splitext(os.path.basename(image_path))[0]
    out_path = os.path.join(output_dir, f"{image_basename}_plus1_revoke.jpg")

    # 加载底图
    orig = Image.open(image_path).convert("RGB")
    ow, oh = orig.size

    font_sm = _get_debug_font(16)
    font_md = _get_debug_font(20)
    font_lg = _get_debug_font(26)

    banner_h = 140
    canvas = Image.new("RGB", (ow, oh + banner_h), (255, 255, 255))
    canvas.paste(orig, (0, banner_h))
    draw = ImageDraw.Draw(canvas)

    # ---- 信息栏 ----
    yc = 8
    # 行1：标题
    draw.text((10, yc), "末行误判校验 (Plus1 Revoke Check)",
              fill=(0, 0, 0), font=font_md)
    yc += 26

    # 行2：参数
    draw.text((10, yc),
              f"eps_ratio_check={eps_ratio_check:.4f}  "
              f"阈值(2×eps_ratio)={threshold:.4f}  "
              f"图像高度={img_height}px",
              fill=(50, 50, 50), font=font_sm)
    yc += 22

    # 行3：段0最后一行
    draw.text((10, yc),
              f"段0最后一行({voted_N}): {len(seg0_last_polys)}个裁剪多边形  "
              f"平均Y={mean_y_last:.1f}",
              fill=(0, 80, 200), font=font_sm)
    yc += 22

    # 行4：动态参考段的第N行
    draw.text((10, yc),
              f"参考段{reference_segment_indices}第{voted_N}行: "
              f"{len(later_last_polys)}个裁剪多边形  "
              f"总体平均Y={mean_y_last_ref:.1f}",
              fill=(0, 150, 50), font=font_sm)
    yc += 22

    # 行5：判定
    diff_px = abs(mean_y_last - mean_y_last_ref)
    judge = "误判 → 撤销+1" if diff_ratio < threshold else "非误判 → 保留+1"
    judge_color = (200, 120, 0) if diff_ratio < threshold else (0, 120, 0)
    draw.text((10, yc),
              f"差值={diff_px:.1f}px  ratio={diff_ratio:.4f}  "
              f"{'<' if diff_ratio < threshold else '>='} {threshold:.4f}  "
              f"→ {judge}  最终行数={final_N}",
              fill=judge_color, font=font_sm)

    # ---- 图例（右对齐）----
    lx = ow - 350
    ly = 8
    # 蓝色
    draw.rectangle([lx, ly, lx+14, ly+14], fill=(0, 80, 200))
    draw.text((lx+18, ly), "段0-末行(要校验的行)", fill=(50, 50, 50), font=font_sm)
    ly += 18
    # 绿色
    draw.rectangle([lx, ly, lx+14, ly+14], fill=(0, 180, 80))
    draw.text((lx+18, ly), "参考段第N行(参照)", fill=(50, 50, 50), font=font_sm)
    ly += 18
    # 中心点
    draw.ellipse([lx+4, ly+4, lx+12, ly+12], fill=(255, 0, 0))
    draw.text((lx+18, ly), "多边形中心点Y", fill=(50, 50, 50), font=font_sm)
    ly += 18
    # 平均线
    draw.line([(lx, ly+6), (lx+30, ly+6)], fill=(255, 0, 0), width=3)
    draw.text((lx+35, ly), "平均Y线(段0末行)", fill=(50, 50, 50), font=font_sm)
    ly += 18
    draw.line([(lx, ly+6), (lx+30, ly+6)], fill=(0, 200, 0), width=3)
    draw.text((lx+35, ly), "平均Y线(参照组)", fill=(50, 50, 50), font=font_sm)

    # ---- 分段边界虚线 ----
    n_segments = 10
    seg_width = img_width / n_segments
    for s in range(n_segments):
        x = s * seg_width
        for y in range(banner_h, banner_h + oh, 12):
            draw.line([(x, y), (x, min(y + 6, banner_h + oh))],
                      fill=(220, 220, 220), width=1)
    draw.line([(img_width, banner_h), (img_width, banner_h + oh)],
              fill=(220, 220, 220), width=1)

    # ---- 绘制段0末行多边形（蓝色）----
    for idx, (poly, label) in enumerate(zip(seg0_last_polys, seg0_last_labels)):
        pts = [(float(p[0]), float(p[1]) + banner_h) for p in poly]
        if len(pts) >= 3:
            draw.polygon(pts, fill=(0, 80, 200, 60), outline=(0, 60, 180), width=2)
        # 中心点
        ys = [p[1] for p in poly]
        cx = float(np.mean([p[0] for p in poly]))
        cy = (max(ys) + min(ys)) / 2.0
        r = 5
        draw.ellipse([cx-r, cy+banner_h-r, cx+r, cy+banner_h+r],
                     fill=(255, 0, 0))
        # Y值标注
        val = f"Y={cy:.0f}"
        tb = draw.textbbox((0, 0), val, font=font_sm)
        tw = tb[2] - tb[0]
        draw.rectangle(
            [cx - tw/2 - 2, cy + banner_h - 14, cx + tw/2 + 2, cy + banner_h - 2],
            fill=(255, 255, 255, 220),
        )
        draw.text((cx - tw/2, cy + banner_h - 14), val,
                  fill=(0, 0, 120), font=font_sm)

    # ---- 绘制动态参考段第N行多边形（绿色）----
    for idx, (poly, label) in enumerate(zip(later_last_polys, later_last_labels)):
        pts = [(float(p[0]), float(p[1]) + banner_h) for p in poly]
        if len(pts) >= 3:
            draw.polygon(pts, fill=(0, 180, 80, 60), outline=(0, 140, 50), width=2)
        # 中心点
        ys = [p[1] for p in poly]
        cx = float(np.mean([p[0] for p in poly]))
        cy = (max(ys) + min(ys)) / 2.0
        r = 5
        draw.ellipse([cx-r, cy+banner_h-r, cx+r, cy+banner_h+r],
                     fill=(0, 200, 0))
        # Y值标注
        val = f"Y={cy:.0f}"
        tb = draw.textbbox((0, 0), val, font=font_sm)
        tw = tb[2] - tb[0]
        draw.rectangle(
            [cx - tw/2 - 2, cy + banner_h + 4, cx + tw/2 + 2, cy + banner_h + 16],
            fill=(255, 255, 255, 220),
        )
        draw.text((cx - tw/2, cy + banner_h + 4), val,
                  fill=(0, 100, 0), font=font_sm)

    # ---- 平均Y线 ----
    # 段0末行平均Y（红色实线）
    y_last = mean_y_last + banner_h
    draw.line([(0, y_last), (img_width, y_last)],
              fill=(255, 0, 0), width=3)
    label = f"meanY(末行)={mean_y_last:.1f}"
    tb = draw.textbbox((0, 0), label, font=font_sm)
    tw = tb[2] - tb[0]
    th = tb[3] - tb[1]
    draw.rectangle(
        [4, y_last - th - 4, tw + 14, y_last + 2],
        fill=(255, 220, 220), outline=(255, 0, 0),
    )
    draw.text((8, y_last - th - 3), label, fill=(200, 0, 0), font=font_sm)

    # 动态参考段第N行总体平均Y（绿色虚线）
    y_ref = mean_y_last_ref + banner_h
    for dash_y in range(banner_h, banner_h + oh, 16):
        draw.line([(0, min(dash_y, y_ref)),
                   (min(dash_y + 8, y_ref), y_ref)],
                  fill=(0, 180, 80), width=3)
    # Also draw as a full line overlay for visibility
    for x in range(0, img_width, 12):
        if (x // 12) % 2 == 0:
            draw.line([(x, y_ref), (min(x + 6, img_width), y_ref)],
                      fill=(0, 180, 80), width=2)
    label2 = f"meanY(参照)={mean_y_last_ref:.1f}"
    tb2 = draw.textbbox((0, 0), label2, font=font_sm)
    tw2 = tb2[2] - tb2[0]
    draw.rectangle(
        [4, y_ref - th - 4, tw2 + 14, y_ref + 2],
        fill=(220, 255, 220), outline=(0, 180, 80),
    )
    draw.text((8, y_ref - th - 3), label2, fill=(0, 120, 0), font=font_sm)

    # ---- 保存 ----
    canvas.save(out_path, "JPEG", quality=90)
    print(f"[分段投影] 误判校验可视化图已保存: {out_path}")


def assign_cores_to_runs_scanning(boxes, tag_boxes, row_labels, row_count,
                                  x_tolerance_ratio=0.1, row_centerlines=None):
    """
    逐行扫描，回次只有在遇到岩芯牌时才会停止，未遇到就会一直继续

    核心逻辑：
    1. 全局只有一个回次计数器，连续递增，不重置
    2. 每个岩芯牌标记一个回次的结束
    3. 回次数量 = 岩芯牌总数 + 1
    4. 没有牌的行，所有岩芯属于当前回次

    参数:
        boxes: 岩芯检测框列表
        tag_boxes: 岩芯牌检测框列表
        row_labels: 行聚类结果
        row_count: 总行数
        x_tolerance_ratio: X坐标容差比例（暂未使用）
        row_centerlines: 各行随 X 变化的中心线。提供时，牌按「该牌 X 处中心线 Y」贴行，
                         与岩芯归属使用同一套几何；缺省时退化为全行平均 Y（仅直箱可靠）。

    返回:
        run_labels: 每段岩芯对应的回次标签
        run_info: 每个回次的信息
    """
    if len(boxes) == 0:
        return [-1] * len(boxes), []

    if len(tag_boxes) == 0:
        run_labels = [0] * len(boxes)
        run_info = [{'row': -1, 'run_index': 0, 'tag_x': None, 'region': 'no_tag'}]
        return run_labels, run_info

    # ========== 1. 为每个岩芯牌分配行标签 ==========
    eval_row = _make_centerline_evaluator(row_centerlines) if row_centerlines else None
    tag_row_labels = []
    tag_centers = [_tag_center(tag) for tag in tag_boxes]
    for tag_c in tag_centers:
        if tag_c is None:
            tag_row_labels.append(-1)
            continue
        tag_x, tag_center_y = tag_c
        min_dist = float('inf')
        assigned_row = -1
        if eval_row is not None:
            for row_id in range(row_count):
                yr = eval_row(row_id, tag_x)
                if yr is None:
                    continue
                dist = abs(tag_center_y - yr)
                if dist < min_dist:
                    min_dist = dist
                    assigned_row = row_id
        else:
            for row_id in range(row_count):
                row_indices = [i for i, label in enumerate(row_labels) if label == row_id]
                if not row_indices:
                    continue
                row_y_values = []
                for i in row_indices:
                    y = [box[1] for box in boxes[i]]
                    row_y_values.append((max(y) + min(y)) / 2.0)
                avg_row_y = np.mean(row_y_values)
                dist = abs(tag_center_y - avg_row_y)
                if dist < min_dist:
                    min_dist = dist
                    assigned_row = row_id
        tag_row_labels.append(assigned_row)

    # 按行分组岩芯牌，每行内按X排序
    tag_groups = [[] for _ in range(row_count)]
    for i, row_id in enumerate(tag_row_labels):
        if row_id != -1:
            tag_groups[row_id].append(i)

    for row_id in range(row_count):
        tag_groups[row_id] = sorted(
            tag_groups[row_id],
            key=lambda i: tag_centers[i][0] if tag_centers[i] else 0.0
        )

    # ========== 2. 获取所有行的岩芯牌总数 ==========
    total_tags = sum(len(g) for g in tag_groups)
    total_runs = total_tags + 1  # 回次数量 = 岩芯牌总数 + 1

    print(f"岩芯牌总数: {total_tags} → 回次数量: {total_runs}")

    # ========== 3. 逐行扫描分配回次 ==========
    run_labels = [-1] * len(boxes)
    run_info = []
    run_counter = 0  # 当前回次编号

    for row_id in range(row_count):
        # 获取该行的岩芯并按X排序
        core_indices = [i for i, label in enumerate(row_labels) if label == row_id]
        if not core_indices:
            continue

        def sort_key(i):
            x = [box[0] for box in boxes[i]]
            return (min(x) + max(x)) / 2.0
        core_indices_sorted = sorted(
            core_indices,
            key=sort_key
        )

        # 获取该行的岩芯牌
        tags_in_row = tag_groups[row_id]

        # 获取该行岩芯的X范围（使用所有顶点的实际X范围，而非中心点范围）
        all_vertex_x = []
        for i in core_indices_sorted:
            for vertex in boxes[i]:
                all_vertex_x.append(vertex[0])
        row_start_x = min(all_vertex_x) if all_vertex_x else 0
        row_end_x = max(all_vertex_x) if all_vertex_x else 0

        if not tags_in_row:
            # 该行没有岩芯牌：所有岩芯归入当前回次
            for idx in core_indices_sorted:
                run_labels[idx] = run_counter

            existing = [info for info in run_info
                        if info['run_index'] == run_counter and info['row'] == row_id]
            if not existing:
                run_info.append({
                    'row': row_id,
                    'run_index': run_counter,
                    'tag_x': None,
                    'region': f'[{row_start_x:.0f}~{row_end_x:.0f}]',
                    'left_bound': row_start_x,
                    'right_bound': row_end_x
                })
            # 没有牌，不回次计数器，继续当前回次
            continue

        # 有岩芯牌：获取牌X坐标
        tag_x_positions = [
            tag_centers[i][0] for i in tags_in_row if tag_centers[i] is not None
        ]
        n_tags = len(tag_x_positions)

        # 构建分割点并排序（防止牌X坐标超出岩芯实际范围导致乱序）
        split_points = sorted([row_start_x] + tag_x_positions + [row_end_x])

        # 分配岩芯到回次
        for idx in core_indices_sorted:
            x = [box[0] for box in boxes[idx]]
            core_x = (min(x) + max(x)) / 2.0
            # core_x = (boxes[idx][0] + boxes[idx][2]) / 2

            assigned_offset = -1
            for offset in range(len(split_points) - 1):
                left = split_points[offset]
                right = split_points[offset + 1]

                if offset == len(split_points) - 2:
                    if left <= core_x <= right:
                        assigned_offset = offset
                        break
                else:
                    if left <= core_x < right:
                        assigned_offset = offset
                        break

            if assigned_offset == -1:
                min_dist = float('inf')
                for offset in range(len(split_points) - 1):
                    mid = (split_points[offset] + split_points[offset + 1]) / 2
                    dist = abs(core_x - mid)
                    if dist < min_dist:
                        min_dist = dist
                        assigned_offset = offset

            # 回次编号 = 当前回次 + 偏移量
            run_id = run_counter + assigned_offset
            run_labels[idx] = run_id

        # 记录该行的回次信息
        tag_x_set = set(tag_x_positions)
        for offset in range(len(split_points) - 1):
            run_id = run_counter + offset
            left = split_points[offset]
            right = split_points[offset + 1]
            # 右边界是牌X则标记为该回次的结束牌
            tag_x = right if right in tag_x_set else None

            existing = [info for info in run_info if info['run_index'] == run_id and info['row'] == row_id]
            if not existing:
                run_info.append({
                    'row': row_id,
                    'run_index': run_id,
                    'tag_x': tag_x,
                    'region': f'[{left:.0f}~{right:.0f}]',
                    'left_bound': left,
                    'right_bound': right
                })

        # 更新回次计数器：该行有n个牌，新增 n 个回次（因为最后一个牌之后的回次会继续到下一行）
        run_counter += n_tags

    # ========== 4. 清理未分配的岩芯 ==========
    for i, label in enumerate(run_labels):
        if label == -1:
            x_coords = [p[0] for p in boxes[i]]
            core_x = (min(x_coords) + max(x_coords)) / 2.0
            min_dist = float('inf')
            nearest_run = 0
            for info in run_info:
                dist = abs(core_x - info['left_bound'])
                if dist < min_dist:
                    min_dist = dist
                    nearest_run = info['run_index']
            run_labels[i] = nearest_run

    return run_labels, run_info

def print_run_summary_merged(run_labels, run_info, boxes, row_labels, row_count):
    """
    打印回次划分结果
    """
    from collections import defaultdict

    run_groups = defaultdict(list)
    for i, run_id in enumerate(run_labels):
        if run_id != -1:
            run_groups[run_id].append(i)

    sorted_run_ids = sorted(run_groups.keys())

    print("\n" + "=" * 80)
    print("回次划分结果（遇牌则回次+1，无牌行继续当前回次）".center(80))
    print("=" * 80)
    print(f"回次数量: {len(sorted_run_ids)} (岩芯牌总数 + 1)")
    print("-" * 80)

    # 按行显示
    rows_with_info = {}
    for info in run_info:
        row_id = info['row']
        if row_id not in rows_with_info:
            rows_with_info[row_id] = []
        rows_with_info[row_id].append(info)

    for row_id in sorted(rows_with_info.keys()):
        row_infos = sorted(rows_with_info[row_id], key=lambda x: x['run_index'])
        row_indices = [i for i, label in enumerate(row_labels) if label == row_id]

        depth_label = "浅" if row_id == 0 else "深" if row_id == row_count - 1 else "中"
        print(f"\n【第 {row_id + 1} 行】（{depth_label}部）共 {len(row_indices)} 段岩芯")
        print(f"{'回次':<10} {'X区间':<22} {'岩芯牌X':<14} {'岩芯段数':<10}")
        print("-" * 60)

        for info in row_infos:
            run_id = info['run_index']
            region = info.get('region', f"[{info['left_bound']:.0f}~{info['right_bound']:.0f}]")
            tag_x = info.get('tag_x', None)
            tag_x_str = f"{tag_x:.0f}" if tag_x is not None else "无"

            core_count = sum(1 for i in row_indices if run_labels[i] == run_id)

            if core_count > 0:
                print(f"  R{run_id:<8} {region:<22} {tag_x_str:<14} {core_count}")

    print("\n" + "=" * 80)


def visualize_run_results_by_row(img, boxes, tag_boxes, row_labels, run_labels, run_info,
                                 rqd_results=None, image_width=None, box_length_mm=None,
                                 min_length_mm=100, tag=''):
    """
    可视化回次划分结果（黄色高亮RQD有效岩芯）

    参数:
        img: 原始图像（numpy数组）
        boxes: 岩芯检测框列表
        tag_boxes: 岩芯牌检测框列表
        row_labels: 行聚类结果
        run_labels: 回次标签列表
        run_info: 回次信息列表
        rqd_results: calculate_rqd_by_run 返回的结果（用于获取有效岩芯列表）
        image_width: 图像总像素宽度（用于计算长度）
        box_length_mm: 总像素对应的实际长度（mm）
        min_length_mm: RQD统计的最小长度阈值（默认100mm）
    """

    fig, ax = plt.subplots(1, 1, figsize=(16, 10))

    # 显示背景图像
    if img is not None:
        ax.imshow(img)

    unique_runs = sorted(set([l for l in run_labels if l != -1]))
    if not unique_runs:
        ax.set_title('无回次划分结果')
        ax.set_xlabel('X (像素)')
        ax.set_ylabel('Y (像素)')
        plt.tight_layout()
        plt.show()
        return

    # 颜色映射（回次颜色）
    colors = plt.cm.tab20(np.linspace(0, 1, max(len(unique_runs), 1)))
    run_color_map = {run_id: colors[i] for i, run_id in enumerate(unique_runs)}

    # ========== 构建有效岩芯集合 ==========
    valid_core_indices = set()
    if rqd_results is not None:
        for result in rqd_results:
            for idx in result.get('valid_core_indices', []):
                valid_core_indices.add(idx)

    # ========== 绘制岩芯 ==========
    for i, box in enumerate(boxes):
        x = [p[0] for p in box]
        y = [p[1] for p in box]
        x1, y1, x2, y2 = min(x), min(y), max(x), max(y)
        run_id = run_labels[i]
        color = run_color_map.get(run_id, 'gray')

        # 判断是否为有效岩芯（长度≥min_length_mm）
        is_valid = i in valid_core_indices

        # 计算岩芯长度
        pixel_width = x2 - x1
        if image_width is not None and box_length_mm is not None:
            scale_mm_per_pixel = box_length_mm / image_width
            length_mm = pixel_width * scale_mm_per_pixel
            length_str = f"{length_mm:.0f}mm"
        else:
            length_str = f"{pixel_width:.0f}px"

        # ===== 绘制矩形框 =====
        if is_valid:
            # 有效岩芯：黄色高亮（粗边框 + 半透明黄色填充）
            # 外发光效果（多层边框）
            for j in range(3, 0, -1):
                rect = patches.Rectangle(
                    (x1 - j, y1 - j), (x2 - x1) + 2 * j, (y2 - y1) + 2 * j,
                    linewidth=1, edgecolor='gold', facecolor='none', alpha=0.3
                )
                ax.add_patch(rect)

            # 黄色高亮填充
            rect = patches.Rectangle(
                (x1, y1), x2 - x1, y2 - y1,
                linewidth=4, edgecolor='gold', facecolor='yellow', alpha=0.4
            )
            ax.add_patch(rect)

            # 内部填充（回次颜色）
            rect = patches.Rectangle(
                (x1, y1), x2 - x1, y2 - y1,
                linewidth=2, edgecolor='gold', facecolor=color, alpha=0.3
            )
            ax.add_patch(rect)
        else:
            # 无效岩芯：普通显示
            rect = patches.Rectangle(
                (x1, y1), x2 - x1, y2 - y1,
                linewidth=2, edgecolor=color, facecolor=color, alpha=0.35
            )
            ax.add_patch(rect)
            rect = patches.Rectangle(
                (x1, y1), x2 - x1, y2 - y1,
                linewidth=2, edgecolor=color, facecolor='none'
            )
            ax.add_patch(rect)

        # ===== 显示标签 =====
        if is_valid:
            # 有效岩芯：显示 ✓ 和长度
            label = f'✓ R{run_id}'
            label2 = length_str
            bbox_props = dict(boxstyle='round', facecolor='gold', alpha=0.9, edgecolor='orange', linewidth=2)
            ax.text((x1 + x2) / 2, (y1 + y2) / 2 - 5, label,
                    color='black', fontsize=8, ha='center', va='bottom',
                    weight='bold', bbox=bbox_props)
            ax.text((x1 + x2) / 2, (y1 + y2) / 2 + 12, label2,
                    color='darkorange', fontsize=6, ha='center', va='top',
                    weight='bold')
        else:
            # 无效岩芯：只显示回次编号
            label = f'R{run_id}'
            bbox_props = dict(boxstyle='round', facecolor='white', alpha=0.7)
            ax.text((x1 + x2) / 2, (y1 + y2) / 2, label,
                    color='black', fontsize=7, ha='center', va='center',
                    weight='bold', bbox=bbox_props)

    # ========== 绘制岩芯牌 ==========
    for tag in tag_boxes:
        x1, y1, x2, y2 = tag[:4]
        tag_x = (x1 + x2) / 2
        tag_y = (y1 + y2) / 2
        ax.plot(tag_x, tag_y, '*', color='red', markersize=18)
        rect = patches.Rectangle(
            (x1, y1), x2 - x1, y2 - y1,
            linewidth=2, edgecolor='red', facecolor='none', linestyle='--'
        )
        ax.add_patch(rect)
        ax.text(tag_x, tag_y - 15, '牌', color='red', fontsize=8,
                ha='center', va='top', weight='bold')

    # ========== 绘制回次分界线 ==========
    if run_info:
        for info in run_info:
            if 'right_bound' in info and info.get('tag_x') is not None:
                x = info['right_bound']
                ax.axvline(x=x, color='cyan', linestyle='-', linewidth=1.5, alpha=0.4)

    # ========== 图例 ==========
    handles = []

    # 回次图例（只显示前10个）
    for run_id in unique_runs[:10]:
        handles.append(patches.Patch(color=run_color_map[run_id], label=f'回次 {run_id}'))

    # 岩芯牌图例
    handles.append(plt.Line2D([0], [0], marker='*', color='red', markersize=12,
                              linestyle='None', label='岩芯牌'))

    # 有效岩芯图例（黄色高亮）
    valid_patch = patches.Patch(facecolor='yellow', edgecolor='gold', linewidth=3,
                                alpha=0.6, label='✓ 纳入RQD (≥100mm)')
    handles.append(valid_patch)

    # 无效岩芯图例
    invalid_patch = patches.Patch(facecolor='gray', edgecolor='gray', linewidth=2,
                                  alpha=0.4, label='未纳入RQD (<100mm)')
    handles.append(invalid_patch)

    ax.legend(handles=handles, loc='upper left', fontsize=9, ncol=2, framealpha=0.9)

    # ========== 设置坐标轴 ==========
    ax.set_xlabel('X (像素)')
    ax.set_ylabel('Y (像素)')
    ax.invert_yaxis()

    if img is not None:
        ax.set_xlim(0, img.shape[1])
        ax.set_ylim(img.shape[0], 0)
    else:
        all_x, all_y = [], []
        for box in boxes:
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            all_x.extend([min(xs), max(xs)])
            all_y.extend([min(ys), max(ys)])
        if all_x and all_y:
            margin = 20
            ax.set_xlim(min(all_x) - margin, max(all_x) + margin)
            ax.set_ylim(max(all_y) + margin, min(all_y) - margin)

    # ========== 添加统计信息 ==========
    if rqd_results is not None:
        total_cores = sum(r['core_count'] for r in rqd_results)
        total_valid = sum(r['valid_length_mm'] for r in rqd_results)
        total_length = sum(r['total_length_mm'] for r in rqd_results)
        overall_rqd = (total_valid / total_length * 100) if total_length > 0 else 0

        info_text = (
            f"回次数: {len(rqd_results)} | "
            f"岩芯总数: {total_cores} | "
            f"有效岩芯: {len(valid_core_indices)} | "
            f"RQD: {overall_rqd:.1f}%"
        )
        ax.text(0.02, 0.02, info_text, transform=ax.transAxes,
                fontsize=12, weight='bold', color='black',
                bbox=dict(boxstyle='round', facecolor='white', alpha=0.85))

    # ========== 标题 ==========
    title = '岩芯回次划分（黄色高亮 = 纳入RQD的有效岩芯 ≥100mm）'
    ax.set_title(title, fontsize=14, weight='bold')
    ax.grid(True, alpha=0.15)
    plt.tight_layout()
    plt.savefig('./2.jpg', dpi=300, bbox_inches='tight')
    plt.show()


if __name__ == '__main__':
    image_path = r'D:\Python\项目\ImageRecognition\Test\TestImages\rock\images\train\0byHBbOq11G5oci6fweB.jpg'
    detection_boxes = runCoreModel_OutputCoreBoxCoor(image_path)

    with Image.open(image_path) as _im:
        img_w, img_h = _im.size

    row_count, row_labels, centers, centerlines = detect_rows_by_segmented_projection(
        boxes=detection_boxes,
        img_width=img_w,
        img_height=img_h,
        image_path=image_path,
    )

    print(f"检测到 {row_count} 行岩芯")
    print(f"各行中心Y坐标: {[round(c, 1) for c in centers]}")
    print(f"每框行标签: {row_labels}")

    visualize_row_clustering(
        boxes=detection_boxes,
        row_labels=row_labels,
        row_centers=centers,
        title=f"岩芯行聚类结果（{row_count}行）"
    )

    card_boxes_xy = [
        [1329, 945], [2856, 1339], [3239, 2250]
    ]
    run_labels, run_info = assign_cores_to_runs_scanning(
        detection_boxes, card_boxes_xy, row_labels, row_count,
        row_centerlines=centerlines,
    )

    print('run_labels:', run_labels)
    print('run_info:', run_info)

    print_run_summary_merged(run_labels, run_info, detection_boxes, row_labels, row_count)

    card_boxes = []
    for box in card_boxes_xy:
        card_boxes.append([box[0] - 25, box[1] - 50, box[0] + 25, box[1] + 50])
    img = cv2.imread(image_path)
    visualize_run_results_by_row(img, detection_boxes, card_boxes, row_labels, run_labels, run_info, tag='runs')

