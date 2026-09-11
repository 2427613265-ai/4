# -*- coding: utf-8 -*-
"""逻辑闭合后的行检测：弯曲归属、两端不满末行、假 +1 撤销。"""
import os
import sys

os.environ.setdefault("MPLBACKEND", "Agg")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import DetectCoreRows as dcr


def rect(x1, y1, x2, y2):
    return [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]


W, H = 2000, 1200
ROW_YS = [150, 400, 650, 900]
PITCH = 250


def make_rows(row_ys, drop=0.0, x_min=40, x_max=1960, step=90, half_h=40, half_w=40):
    boxes, truth = [], []
    for r, y0 in enumerate(row_ys):
        for x in range(x_min, x_max, step):
            y = y0 + drop * (x / float(W))
            boxes.append(rect(x, y - half_h, x + half_w * 2, y + half_h))
            truth.append(r)
    return boxes, truth


def run(boxes):
    return dcr.detect_rows_by_segmented_projection(boxes, W, H, image_path=None)


def test_straight_four_rows():
    boxes, truth = make_rows(ROW_YS)
    rc, labels, _, _ = run(boxes)
    assert rc == 4
    assert labels == truth


def test_bent_assignment_survives_beyond_half_pitch():
    """弯幅超过半个行距时，全图一维 KMeans 会错分；中心线归属应仍正确。"""
    for drop in (220, 320, 400):
        boxes, truth = make_rows(ROW_YS, drop=drop)
        rc, labels, _, _ = run(boxes)
        wrong = sum(1 for a, b in zip(labels, truth) if a != b)
        assert rc == 4, f"drop={drop} row_count={rc}"
        assert wrong == 0, f"drop={drop} wrong={wrong}/{len(boxes)}"


def test_partial_last_row_on_left():
    boxes, truth = make_rows(ROW_YS[:3])
    extra, extra_t = make_rows([900], x_min=40, x_max=360)
    extra_t = [3] * len(extra)
    boxes += extra
    truth += extra_t
    rc, labels, _, _ = run(boxes)
    assert rc == 4
    assert labels == truth


def test_partial_last_row_on_right():
    boxes, truth = make_rows(ROW_YS[:3])
    extra, _ = make_rows([900], x_min=1640, x_max=1960)
    boxes += extra
    truth += [3] * len(extra)
    rc, labels, _, _ = run(boxes)
    assert rc == 4, f"right partial last should count as 4, got {rc}"
    assert labels == truth


def test_false_plus1_small_offset_revoked():
    boxes, truth = make_rows(ROW_YS[:3])
    frag, _ = make_rows([675], x_min=40, x_max=180, step=70, half_h=25, half_w=25)
    boxes += frag
    rc, labels, _, _ = run(boxes)
    assert rc == 3
    assert labels[:len(truth)] == truth


def test_single_row():
    boxes, truth = make_rows([400])
    rc, labels, _, _ = run(boxes)
    assert rc == 1
    assert labels == truth


def test_bent_plus_partial_left():
    boxes, truth = make_rows(ROW_YS[:3], drop=220)
    extra, _ = make_rows([900], drop=220, x_min=40, x_max=360)
    boxes += extra
    truth += [3] * len(extra)
    rc, labels, _, _ = run(boxes)
    assert rc == 4
    wrong = sum(1 for a, b in zip(labels, truth) if a != b)
    assert wrong == 0, f"wrong={wrong}/{len(boxes)} labels vs truth mismatch"


def test_tag_uses_centerline_on_bent_row():
    boxes, truth = make_rows(ROW_YS, drop=320)
    rc, labels, _, centerlines = run(boxes)
    assert rc == 4
    # 右侧第 0 行上的牌：全局行均值会贴到第 1 行，中心线应仍贴第 0 行
    right_x = 1930
    right_y = 150 + 320 * (right_x / float(W))
    tags = [[right_x, right_y]]
    _, run_info = dcr.assign_cores_to_runs_scanning(
        boxes, tags, labels, rc, row_centerlines=centerlines
    )
    assert any(info.get("row") == 0 for info in run_info), run_info


def test_tag_xyxy_format_accepted():
    boxes, _ = make_rows(ROW_YS[:1])
    rc, labels, _, centerlines = run(boxes)
    tags = [[100, 350, 140, 450]]
    run_labels, _ = dcr.assign_cores_to_runs_scanning(
        boxes, tags, labels, rc, row_centerlines=centerlines
    )
    assert len(run_labels) == len(boxes)


def test_eps_uses_full_image_height():
    boxes, _ = make_rows(ROW_YS)
    ratio = 0.0267
    rc_span, _, _ = dcr.detect_rows_from_boxes(boxes, eps_ratio=ratio, min_samples=1)
    rc_img, _, _ = dcr.detect_rows_from_boxes(
        boxes, eps_ratio=ratio, min_samples=1, img_height=H
    )
    assert rc_img == 4
    assert rc_span == 4


if __name__ == "__main__":
    tests = [
        test_straight_four_rows,
        test_bent_assignment_survives_beyond_half_pitch,
        test_partial_last_row_on_left,
        test_partial_last_row_on_right,
        test_false_plus1_small_offset_revoked,
        test_single_row,
        test_bent_plus_partial_left,
        test_tag_uses_centerline_on_bent_row,
        test_tag_xyxy_format_accepted,
        test_eps_uses_full_image_height,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    if failed:
        raise SystemExit(f"{failed}/{len(tests)} failed")
    print(f"{len(tests)}/{len(tests)} passed")

