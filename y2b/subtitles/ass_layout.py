#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ASS/SRT 字幕排版、换行和烧录用样式。

语言检测也放在这里。质检门控和翻译流程仍留在 task_manager。
外观解析函数经 task_manager 门面调用，以保留对
``parse_style_overrides`` 的既有 monkeypatch。
"""

import logging
import os
import shutil
import unicodedata

logger = logging.getLogger("task_manager")


def _facade_call(name):
    def _call(*args, **kwargs):
        import y2b.pipeline.task_manager as task_manager
        return getattr(task_manager, name)(*args, **kwargs)

    _call.__name__ = name
    _call.__qualname__ = name
    return _call


parse_style_overrides = _facade_call("parse_style_overrides")
apply_style_overrides = _facade_call("apply_style_overrides")
diagnose_style = _facade_call("diagnose_style")
get_app_subdir = _facade_call("get_app_subdir")


class SubtitleLayoutMixin:
    
    def _detect_subtitle_language(self, subtitle_path):
        """从字幕文件名提取语言代码（如 video.ja.srt -> ja）"""
        try:
            filename = os.path.basename(subtitle_path)
            name_without_ext = os.path.splitext(filename)[0]
            parts = name_without_ext.split('.')
            if len(parts) >= 2:
                lang_code = parts[-1].lower()
                if '-' in lang_code:
                    lang_code = lang_code.split('-')[0]
                return lang_code
            return "auto"
        except Exception:
            return "auto"

    _ASS_PLAY_RES_X = 1920
    _ASS_PLAY_RES_Y = 1080
    #: force_style 比生成 ASS 文档多需要的键：生成路径把这些值写进 [V4+ Styles]
    #: 的 Style 行，而 ASS/SSA 输入路径必须靠 force_style 逐键覆盖源样式。
    _FORCE_STYLE_APPEARANCE_KEYS = (
        'PrimaryColour',
        'OutlineColour',
        'BackColour',
        'BorderStyle',
        'Bold',
    )

    _ASS_STYLE_BASE = {
        'FontSize': 56.0,
        'Outline': 2.0,
        'Shadow': 1.0,
        'MarginV': 40.0,
        'MarginL': 96.0,
        'MarginR': 96.0,
        'Alignment': 2,
        # BorderStyle=4 gives a modern rounded-rectangle background box
        # (supported by libass/FFmpeg), which is the dominant look for
        # streaming/online-video captions.
        # BorderStyle=1 with a thick, dark semi-transparent outline.
        # This gives the clean "no-box" streaming caption look while keeping
        # text readable on bright/complex backgrounds.
        'BorderStyle': 1,
        'Bold': 1,
        'PrimaryColour': '&H00FFFFFF',
        'SecondaryColour': '&H00FFFFFF',
        'OutlineColour': '&HB2000000',
        'BackColour': '&H00000000',
    }
    _ASS_LANDSCAPE_FONT_SIZE_ANCHORS = (
        (720.0, 48.0),
        (1080.0, 54.0),
        (1440.0, 68.0),
        (2160.0, 102.0),
    )
    _ASS_PORTRAIT_FONT_SIZE_ANCHORS = (
        (720.0, 44.0),
        (1280.0, 54.0),
        (1920.0, 68.0),
        (2560.0, 76.0),
    )
    _ASS_LANDSCAPE_MARGIN_V_ANCHORS = (
        (720.0, 56.0),
        (1080.0, 62.0),
        (1440.0, 82.0),
        (2160.0, 124.0),
    )
    _ASS_PORTRAIT_MARGIN_V_ANCHORS = (
        (720.0, 120.0),
        (1280.0, 156.0),
        (1920.0, 220.0),
        (2560.0, 292.0),
    )
    # Landscape captions use generous side margins to avoid crowding the
    # edges, but still keep enough width to stay single-line after scaling.
    _ASS_LANDSCAPE_SIDE_MARGIN_RATIO = 0.025
    _ASS_LANDSCAPE_SIDE_MARGIN_MIN = 32.0
    _ASS_LANDSCAPE_SIDE_MARGIN_MAX = 80.0
    _ASS_PORTRAIT_SIDE_MARGIN_RATIO = 0.095
    _ASS_PORTRAIT_SIDE_MARGIN_MIN = 82.0
    _ASS_PORTRAIT_SIDE_MARGIN_MAX = 156.0
    _ASS_LANDSCAPE_LAYOUT_DENSITY = 0.93
    # 单行优先模式允许密度略 >1.0：font_size 按等宽近似估算偏保守，略微放宽
    # 以更充分地利用 _ASS_SAFE_WIDTH_RATIO(0.98) 已留出的安全宽度。实际单行
    # 字符上限由下游 _clamp(18, 28) 钳制，不会真正溢出可用宽度。
    _ASS_LANDSCAPE_SINGLE_LINE_DENSITY = 1.04
    _ASS_LANDSCAPE_SINGLE_LINE_LIMIT_MIN = 28.0
    _ASS_LANDSCAPE_SINGLE_LINE_LIMIT_MAX = 38.0
    # Tolerance (in visual units) above single_line_limit where we still
    # prefer keeping the text on one line rather than splitting.
    _SINGLE_LINE_TOLERANCE = 3.5
    _ASS_PORTRAIT_LAYOUT_DENSITY = 0.92
    # Allow text to use almost the full usable width. A small safety margin
    # remains so descenders/outlines do not touch the screen edges.
    _ASS_SAFE_WIDTH_RATIO = 0.98
    _ASS_OVERRIDE_FONT_SIZE_RATIO_MIN = 0.60
    # Allow aggressive down-scaling for single-line priority. Landscape
    # captions are required to stay on one line, so we shrink the font as
    # far as the global override minimum permits before accepting overflow.
    _ASS_SINGLE_LINE_FONT_SCALE_MIN = 0.60
    _ASS_OVERRIDE_FONT_SIZE_MIN = 32.0
    _ASS_HARD_WRAP_MIN_LINE_LENGTH = 8
    _ASS_PORTRAIT_RESCUE_LINE_LENGTH_MAX = 16.0
    # Thicker outline for the BorderStyle=4 rounded box, plus a soft shadow.
    _ASS_OUTLINE_RATIO = 0.075
    _ASS_OUTLINE_MIN = 2.2
    _ASS_OUTLINE_MAX = 5.5
    _ASS_SHADOW_RATIO = 0.025
    _ASS_SHADOW_MIN = 1.0
    _ASS_SHADOW_MAX = 2.2
    _ASS_OVERRIDE_OUTLINE_RATIO = 0.045
    _ASS_OVERRIDE_OUTLINE_MIN = 1.5
    _ASS_OVERRIDE_OUTLINE_MAX = 3.0
    _STREAMING_SRT_TEMPLATE_HEIGHTS = (720, 1080, 1440, 2160)
    _STREAMING_SRT_STYLE_TEMPLATES = {
        720: {
            'FontSize': 18.0,
            'Outline': 0.4,
            'Shadow': 0.6,
            'MarginL': 42,
            'MarginR': 42,
            'MarginV': 16,
        },
        1080: {
            'FontSize': 18.0,
            'Outline': 0.4,
            'Shadow': 0.6,
            'MarginL': 42,
            'MarginR': 42,
            'MarginV': 16,
        },
        1440: {
            'FontSize': 18.0,
            'Outline': 0.4,
            'Shadow': 0.6,
            'MarginL': 42,
            'MarginR': 42,
            'MarginV': 16,
        },
        2160: {
            'FontSize': 18.0,
            'Outline': 0.4,
            'Shadow': 0.6,
            'MarginL': 42,
            'MarginR': 42,
            'MarginV': 16,
        },
    }
    _STREAMING_SRT_PORTRAIT_FONT_SIZE_ANCHORS = (
        (1280.0, 12.5),
        (1920.0, 12.5),
        (2560.0, 13.5),
    )
    _STREAMING_SRT_PORTRAIT_MARGIN_V_ANCHORS = (
        (1280.0, 18.0),
        (1920.0, 28.0),
        (2560.0, 40.0),
    )
    _STREAMING_SRT_PORTRAIT_SIDE_MARGIN_RATIO = 0.08
    _STREAMING_SRT_LANDSCAPE_LAYOUT_DENSITY = 0.72
    _STREAMING_SRT_PORTRAIT_LAYOUT_DENSITY = 0.96
    _BUNDLED_FONT_EXTENSIONS = ('.otf', '.ttf', '.ttc', '.otc')

    @staticmethod
    def _clamp(value, minimum, maximum):
        return max(minimum, min(maximum, value))

    @staticmethod
    def _interpolate_anchor_value(position, anchors):
        normalized_anchors = []
        for anchor_position, anchor_value in anchors or ():
            try:
                normalized_anchors.append((float(anchor_position), float(anchor_value)))
            except Exception:
                continue

        if not normalized_anchors:
            return 0.0

        normalized_anchors.sort(key=lambda item: item[0])
        if position <= normalized_anchors[0][0]:
            return normalized_anchors[0][1]

        for idx in range(1, len(normalized_anchors)):
            left_position, left_value = normalized_anchors[idx - 1]
            right_position, right_value = normalized_anchors[idx]
            if position <= right_position:
                if right_position <= left_position:
                    return right_value
                ratio = (position - left_position) / (right_position - left_position)
                return left_value + (right_value - left_value) * ratio

        return normalized_anchors[-1][1]

    @classmethod
    def _resolve_ass_dimensions(cls, video_width, video_height):
        try:
            width = int(video_width)
        except Exception:
            width = 0
        try:
            height = int(video_height)
        except Exception:
            height = 0

        if width <= 0:
            width = cls._ASS_PLAY_RES_X
        if height <= 0:
            height = cls._ASS_PLAY_RES_Y
        return width, height

    @staticmethod
    def _format_ass_number(value):
        try:
            value_f = float(value)
        except Exception:
            return str(value)
        if value_f.is_integer():
            return str(int(value_f))
        return f"{value_f:.2f}".rstrip('0').rstrip('.')

    @staticmethod
    def _seconds_to_ass_timestamp(seconds):
        try:
            total_cs = max(0, int(round(float(seconds) * 100)))
        except Exception:
            total_cs = 0
        hours = total_cs // 360000
        total_cs %= 360000
        minutes = total_cs // 6000
        total_cs %= 6000
        secs = total_cs // 100
        centis = total_cs % 100
        return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"

    @staticmethod
    def _escape_ass_text_line(text):
        return str(text or '').replace('\\', r'\\').replace('{', r'\{').replace('}', r'\}')

    @staticmethod
    def _escape_ass_text(text):
        normalized = str(text or '').replace('\r\n', '\n').replace('\r', '\n')
        escaped_lines = []
        for line in normalized.split('\n'):
            escaped_lines.append(SubtitleLayoutMixin._escape_ass_text_line(line))
        return r'\N'.join(escaped_lines)

    @classmethod
    def _compose_ass_dialogue_text(cls, lines, override_font_size=None, override_outline=None, override_shadow=None):
        escaped_lines = [
            cls._escape_ass_text_line(line)
            for line in (lines or [])
            if str(line or '').strip()
        ]
        if not escaped_lines:
            return ''
        payload = r'\N'.join(escaped_lines)
        if override_font_size is None:
            return payload
        try:
            override_font_size = int(round(float(override_font_size)))
        except Exception:
            override_font_size = 0
        if override_font_size <= 0:
            return payload
        tags = [f"\\fs{override_font_size}"]
        for name, value in (("bord", override_outline), ("shad", override_shadow)):
            if value is not None:
                try:
                    number = float(value)
                    if number == number and number not in (float('inf'), float('-inf')):
                        tags.append(f"\\{name}{cls._format_ass_number(number)}")
                except Exception:
                    pass
        return f"{{{''.join(tags)}}}{payload}"

    @classmethod
    def _build_streaming_ass_style(cls, video_width, video_height, style_overrides=None):
        width, height = cls._resolve_ass_dimensions(video_width, video_height)

        style = dict(cls._ASS_STYLE_BASE)
        style.update({
            'PlayResX': width,
            'PlayResY': height,
        })

        is_portrait = height > width
        if is_portrait:
            font_size = cls._clamp(
                cls._interpolate_anchor_value(height, cls._ASS_PORTRAIT_FONT_SIZE_ANCHORS),
                58.0,
                80.0,
            )
            margin_v = cls._clamp(
                cls._interpolate_anchor_value(height, cls._ASS_PORTRAIT_MARGIN_V_ANCHORS),
                150.0,
                320.0,
            )
            side_margin = cls._clamp(
                width * cls._ASS_PORTRAIT_SIDE_MARGIN_RATIO,
                cls._ASS_PORTRAIT_SIDE_MARGIN_MIN,
                cls._ASS_PORTRAIT_SIDE_MARGIN_MAX,
            )
        else:
            font_size = cls._clamp(
                cls._interpolate_anchor_value(height, cls._ASS_LANDSCAPE_FONT_SIZE_ANCHORS),
                46.0,
                132.0,
            )
            margin_v = cls._clamp(
                cls._interpolate_anchor_value(height, cls._ASS_LANDSCAPE_MARGIN_V_ANCHORS),
                46.0,
                156.0,
            )
            side_margin = cls._clamp(
                width * cls._ASS_LANDSCAPE_SIDE_MARGIN_RATIO,
                cls._ASS_LANDSCAPE_SIDE_MARGIN_MIN,
                cls._ASS_LANDSCAPE_SIDE_MARGIN_MAX,
            )

        style.update({
            'FontSize': font_size,
            'Outline': cls._clamp(
                font_size * cls._ASS_OUTLINE_RATIO,
                cls._ASS_OUTLINE_MIN,
                cls._ASS_OUTLINE_MAX,
            ),
            'Shadow': cls._clamp(
                font_size * cls._ASS_SHADOW_RATIO,
                cls._ASS_SHADOW_MIN,
                cls._ASS_SHADOW_MAX,
            ),
            'MarginV': margin_v,
            'MarginL': side_margin,
            'MarginR': side_margin,
        })
        if not style_overrides:
            # 未提供覆盖项时返回原样式，保证默认渲染行为与历史版本逐字段一致。
            return style
        return apply_style_overrides(style, style_overrides)

    @staticmethod
    def _sanitize_ass_font_name(font_family):
        # FontName is embedded in force_style, where commas are field separators.
        return (
            str(font_family or 'Arial')
            .replace('\r', ' ')
            .replace('\n', ' ')
            .replace(',', ' ')
            .replace('\\', '/')
            .strip()
            or 'Arial'
        )

    @classmethod
    def _resolve_streaming_srt_template_height(cls, video_width, video_height):
        _, height = cls._resolve_ass_dimensions(video_width, video_height)
        for template_height in cls._STREAMING_SRT_TEMPLATE_HEIGHTS:
            if height <= template_height:
                return template_height
        return cls._STREAMING_SRT_TEMPLATE_HEIGHTS[-1]

    @classmethod
    def _build_streaming_srt_style_description(cls, font_family, video_width, video_height):
        width, height = cls._resolve_ass_dimensions(video_width, video_height)
        template_height = cls._resolve_streaming_srt_template_height(width, height)
        template = dict(cls._STREAMING_SRT_STYLE_TEMPLATES[template_height])
        is_portrait = height > width
        if is_portrait:
            template.update({
                'FontSize': cls._clamp(
                    cls._interpolate_anchor_value(height, cls._STREAMING_SRT_PORTRAIT_FONT_SIZE_ANCHORS),
                    12.5,
                    13.5,
                ),
                'MarginL': int(round(cls._clamp(width * cls._STREAMING_SRT_PORTRAIT_SIDE_MARGIN_RATIO, 40.0, 88.0))),
                'MarginR': int(round(cls._clamp(width * cls._STREAMING_SRT_PORTRAIT_SIDE_MARGIN_RATIO, 40.0, 88.0))),
                'MarginV': int(round(cls._clamp(
                    cls._interpolate_anchor_value(height, cls._STREAMING_SRT_PORTRAIT_MARGIN_V_ANCHORS),
                    16.0,
                    44.0,
                ))),
            })
        template.update({
            'FontName': cls._sanitize_ass_font_name(font_family),
            'Alignment': 2,
            'BorderStyle': 1,
            'OriginalSize': f"{width}x{height}",
            'TemplateHeight': template_height,
        })
        return template

    @classmethod
    def _build_streaming_srt_force_style(cls, font_family, video_width, video_height):
        style = cls._build_streaming_srt_style_description(font_family, video_width, video_height)
        entries = [
            f"FontName={style['FontName']}",
            f"FontSize={cls._format_ass_number(style['FontSize'])}",
            f"Outline={cls._format_ass_number(style['Outline'])}",
            f"Shadow={cls._format_ass_number(style['Shadow'])}",
            f"MarginL={int(style['MarginL'])}",
            f"MarginR={int(style['MarginR'])}",
            f"MarginV={int(style['MarginV'])}",
            f"Alignment={style['Alignment']}",
            f"BorderStyle={style['BorderStyle']}",
        ]
        payload = ','.join(entries).replace("'", r"\'")
        return f"force_style='{payload}'"

    @classmethod
    def _build_streaming_srt_filter(cls, render_subtitle_name, font_family, video_width, video_height):
        style = cls._build_streaming_srt_style_description(font_family, video_width, video_height)
        filter_segments = [
            f"subtitles={render_subtitle_name}",
            f"original_size={style['OriginalSize']}",
            "wrap_unicode=1",
            "fontsdir=fonts",
            "charenc=UTF-8",
            cls._build_streaming_srt_force_style(font_family, video_width, video_height),
        ]
        return ':'.join(filter_segments)

    @classmethod
    def _estimate_streaming_srt_layout_limits(cls, video_width, video_height):
        style = cls._build_streaming_srt_style_description('Arial', video_width, video_height)
        width, height = cls._resolve_ass_dimensions(video_width, video_height)
        is_portrait = height > width
        usable_width = max(
            120.0,
            float(width) - float(style['MarginL']) - float(style['MarginR']),
        )
        font_size = max(1.0, float(style['FontSize']))
        density = (
            cls._STREAMING_SRT_PORTRAIT_LAYOUT_DENSITY
            if is_portrait
            else cls._STREAMING_SRT_LANDSCAPE_LAYOUT_DENSITY
        )
        max_line_length = int(round(usable_width / font_size * density))
        if is_portrait:
            max_line_length = int(cls._clamp(max_line_length, 12.0, 18.0))
            max_lines = 3
        else:
            # Hard-coded single-line target for streaming SRT output.
            max_line_length = int(cls._clamp(max_line_length, 20.0, 26.0))
            max_lines = 1
        return max_line_length, max_lines

    @classmethod
    def _create_streaming_srt_engine(cls, video_width=None, video_height=None):
        from y2b.subtitles.srt_transform_engine import SrtTransformConfig, SrtTransformEngine
        max_line_length, max_lines = cls._estimate_streaming_srt_layout_limits(
            video_width,
            video_height,
        )

        return SrtTransformEngine(
            SrtTransformConfig(
                max_line_length=max_line_length,
                max_lines=max_lines,
                split_long_cues=False,
                preserve_line_breaks=False,
                normalize_punctuation=False,
                filter_filler_words=False,
            ),
            logger=logger,
        )

    @classmethod
    def _wrap_streaming_srt_text(cls, text, video_width, video_height):
        normalized = cls._merge_subtitle_text_parts(
            str(text or '').replace('\r\n', '\n').replace('\r', '\n').split('\n')
        )
        if not normalized:
            return ''

        width, height = cls._resolve_ass_dimensions(video_width, video_height)
        is_portrait = height > width
        style = cls._build_streaming_srt_style_description('Arial', width, height)
        max_line_length, max_lines = cls._estimate_streaming_srt_layout_limits(width, height)
        usable_width = max(
            120.0,
            float(width) - float(style['MarginL']) - float(style['MarginR']),
        )
        font_size = max(1.0, float(style['FontSize']))
        outline = float(style['Outline'])
        shadow = float(style['Shadow'])
        single_line_limit = int(cls._clamp(
            round(usable_width / font_size * cls._ASS_LANDSCAPE_SINGLE_LINE_DENSITY),
            18.0,
            28.0,
        ))
        wrapped_lines = cls._build_wrapped_lines_for_ass(
            normalized,
            is_portrait=is_portrait,
            max_line_length=max_line_length,
            max_lines=max_lines,
            single_line_limit=single_line_limit,
            aggressive=False,
        )
        fits, _, _, _ = cls._check_ass_lines_width_safety(
            wrapped_lines,
            usable_width,
            font_size,
            outline,
            shadow,
        )
        if not fits:
            hard_wrap_lines, _ = cls._find_safe_hard_wrap_lines(
                normalized,
                max_line_length=max_line_length,
                max_lines=max_lines,
                usable_width=usable_width,
                font_size=font_size,
                outline=outline,
                shadow=shadow,
            )
            if hard_wrap_lines:
                wrapped_lines = hard_wrap_lines
        return '\n'.join(line for line in wrapped_lines if line).strip() or normalized

    @classmethod
    def _prepare_streaming_srt_cues(cls, subtitle_text, video_width=None, video_height=None):
        engine = cls._create_streaming_srt_engine(video_width, video_height)
        cues = engine.parse_srt(subtitle_text or '')
        if not cues:
            return []
        total_duration = max(float(cue.get('end', 0.0) or 0.0) for cue in cues)
        # Rendering should preserve validated subtitle content. Hallucination cleanup
        # is part of the ASR generation pipeline and is too destructive here,
        # especially for dense translated Chinese cues that are still legitimate.
        cues = engine.resolve_overlaps(cues, total_duration)
        cues = engine.apply_text_processing(cues)
        cues = engine.finalize_cues(cues, total_duration)
        for cue in cues:
            cue['text'] = cls._wrap_streaming_srt_text(
                cue.get('text', ''),
                video_width,
                video_height,
            )
        return cues

    def _build_streaming_srt_file(
        self,
        subtitle_path,
        srt_output_path,
        task_logger,
        *,
        video_width=None,
        video_height=None,
    ):
        try:
            source_path = subtitle_path
            subtitle_ext = os.path.splitext(subtitle_path)[1].lower()

            if subtitle_ext == '.vtt':
                converted_srt = self._convert_vtt_to_srt(subtitle_path, task_logger)
                if not converted_srt or not os.path.exists(converted_srt):
                    task_logger.error("VTT转SRT失败，无法生成流媒体SRT字幕")
                    return False
                source_path = converted_srt

            with open(source_path, 'r', encoding='utf-8-sig', errors='replace') as subtitle_file:
                subtitle_content = subtitle_file.read()

            cues = self._prepare_streaming_srt_cues(
                subtitle_content,
                video_width=video_width,
                video_height=video_height,
            )
            if not cues:
                task_logger.error(f"未解析出有效字幕条目，无法生成SRT: {os.path.basename(subtitle_path)}")
                return False

            srt_text = self._create_streaming_srt_engine(
                video_width,
                video_height,
            ).render_srt(cues)
            if not srt_text:
                task_logger.error(f"字幕渲染为SRT失败: {os.path.basename(subtitle_path)}")
                return False

            with open(srt_output_path, 'w', encoding='utf-8') as srt_file:
                srt_file.write(srt_text)

            task_logger.info(f"字幕转换为流媒体SRT成功: {srt_output_path}")
            return True
        except Exception as e:
            task_logger.error(f"生成流媒体SRT失败: {str(e)}")
            return False

    @classmethod
    def _build_subtitle_style_description(cls, font_family, video_width, video_height, style_overrides=None):
        style = cls._build_streaming_ass_style(video_width, video_height, style_overrides)
        font_name = cls._sanitize_ass_font_name(font_family)
        force_style = {
            'FontName': font_name,
            'FontSize': cls._format_ass_number(style['FontSize']),
            'Outline': cls._format_ass_number(style['Outline']),
            'Shadow': cls._format_ass_number(style['Shadow']),
            'MarginL': str(int(round(style['MarginL']))),
            'MarginR': str(int(round(style['MarginR']))),
            'MarginV': str(int(round(style['MarginV']))),
            'Alignment': str(style['Alignment']),
        }
        # 颜色 / 粗体 / 底板必须一并写进 force_style：libass 的 force_style 是
        # **逐键覆盖**，只给字体与边距的话，ASS/SSA 源字幕的字色、描边色、粗体与
        # 半透明底板仍旧来自创作者样式 —— 用户改了「字体颜色」画面上毫无变化，
        # 而日志照样打印「将以 force_style 覆盖源样式」（实测：同一份配置喂 .srt
        # 是红字、喂 .ass 仍是白字）。取值已由 ``apply_style_overrides`` 算好，
        # 这里只做搬运，因此默认配置下写入的仍是历史默认值。
        for key in cls._FORCE_STYLE_APPEARANCE_KEYS:
            if key in style:
                force_style[key] = cls._format_ass_number(style[key])
        return style, force_style

    @classmethod
    def _build_subtitle_force_style(cls, font_family, video_width, video_height, style_overrides=None):
        _, force_style = cls._build_subtitle_style_description(
            font_family,
            video_width,
            video_height,
            style_overrides,
        )
        entries = [f"{key}={value}" for key, value in force_style.items()]
        payload = ','.join(entries).replace("'", r"\'")
        return f"force_style='{payload}'"

    @classmethod
    def _estimate_subtitle_layout_limits(cls, video_width, video_height, style_overrides=None):
        style = cls._build_streaming_ass_style(video_width, video_height, style_overrides)
        is_portrait = float(style['PlayResY']) > float(style['PlayResX'])
        usable_width = max(
            120.0,
            float(style['PlayResX']) - float(style['MarginL']) - float(style['MarginR']),
        )
        font_size = max(1.0, float(style['FontSize']))
        density = cls._ASS_PORTRAIT_LAYOUT_DENSITY if is_portrait else cls._ASS_LANDSCAPE_LAYOUT_DENSITY
        max_line_length = int(round(usable_width / font_size * density))
        if is_portrait:
            # Portrait lines are capped at 14 visual units so that 5 balanced
            # lines can absorb medium-length cues without immediately
            # triggering overflow warnings, while still keeping the text narrow.
            max_line_length = int(cls._clamp(max_line_length, 7.0, 14.0))
            # Keep portrait subtitles compact: 5 lines is the hard ceiling,
            # but the partitioner still prefers fewer balanced lines.
            max_lines = 5
        else:
            # Keep short landscape cues on one line, but allow a balanced
            # two-line fallback after the minimum safe font size is reached.
            max_line_length = int(cls._clamp(max_line_length, 18.0, 22.0))
            max_lines = 2
        return max_line_length, max_lines

    @staticmethod
    def _is_cjk_like_char(char):
        if not char:
            return False
        return unicodedata.east_asian_width(char) in {'W', 'F'}

    @classmethod
    def _merge_subtitle_text_parts(cls, parts):
        merged = ''
        trailing_no_space = '([{\u3008\u300a\u300c\u300e\u3010'
        leading_no_space = '.,!?;:)]}，。！？；：、…】）》」』'

        for part in parts or []:
            piece = str(part or '').strip()
            if not piece:
                continue
            if not merged:
                merged = piece
                continue

            prev_char = merged[-1]
            curr_char = piece[0]
            if (
                prev_char.isspace()
                or curr_char.isspace()
                or prev_char in trailing_no_space
                or curr_char in leading_no_space
                or (cls._is_cjk_like_char(prev_char) and cls._is_cjk_like_char(curr_char))
            ):
                merged += piece
            else:
                merged += f" {piece}"

        return merged.strip()

    @classmethod
    def _limit_wrapped_lines(cls, lines, max_lines):
        filtered_lines = [str(line or '').strip() for line in (lines or []) if str(line or '').strip()]
        if len(filtered_lines) <= max_lines:
            return filtered_lines

        if max_lines >= 4:
            visible_lines = filtered_lines[:max_lines - 1]
            remainder = cls._merge_subtitle_text_parts(filtered_lines[max_lines - 1:])
            if remainder:
                visible_lines.append(remainder)
            return [line for line in visible_lines if line]

        merged_text = cls._merge_subtitle_text_parts(filtered_lines)
        if not merged_text or max_lines <= 1:
            return filtered_lines[:max_lines]

        rebalanced_lines = []
        remaining_text = merged_text
        remaining_slots = int(max_lines)

        while remaining_slots > 1 and remaining_text:
            remaining_units = cls._estimate_subtitle_text_units(remaining_text)
            target_line_length = max(6.0, remaining_units / remaining_slots)
            split_index = cls._find_balanced_wrap_index(remaining_text, target_line_length)
            if split_index <= 0:
                fallback_lines = cls._wrap_subtitle_segment_greedily(remaining_text, target_line_length)
                if len(fallback_lines) <= 1:
                    break
                current_line = str(fallback_lines[0] or '').strip()
                next_text = cls._merge_subtitle_text_parts(fallback_lines[1:])
            else:
                current_line = remaining_text[:split_index].strip()
                next_text = remaining_text[split_index:].strip()

            if not current_line or not next_text:
                break

            rebalanced_lines.append(current_line)
            remaining_text = next_text
            remaining_slots -= 1

        if remaining_text:
            rebalanced_lines.append(remaining_text.strip())
        return [line for line in rebalanced_lines[:max_lines] if line]

    @classmethod
    def _build_wrapped_lines_for_ass(
        cls,
        normalized,
        *,
        is_portrait,
        max_line_length,
        max_lines,
        single_line_limit,
        aggressive=False,
    ):
        raw_segments = [segment.strip() for segment in str(normalized or '').split('\n') if segment.strip()]
        wrapped_lines = []

        for segment in raw_segments:
            if is_portrait or aggressive:
                max_lines = max(1, int(max_lines))
                candidate_lines = cls._build_optimal_multiline_partition(
                    segment,
                    max_line_length=max_line_length,
                    min_lines=1,
                    max_lines=max_lines,
                )
                if candidate_lines:
                    wrapped_lines.extend(candidate_lines)
                else:
                    wrapped_lines.extend(cls._wrap_subtitle_segment_greedily(segment, max_line_length))
            elif int(max_lines) <= 1:
                # Hard-coded single-line mode: keep the segment intact so the
                # caller can scale the font or emit an overflow warning instead
                # of wrapping.
                wrapped_lines.append(segment)
            else:
                wrapped_lines.extend(
                    cls._wrap_landscape_segment_for_ass(
                        segment,
                        single_line_limit=single_line_limit,
                        max_line_length=max_line_length,
                    )
                )

        return cls._limit_wrapped_lines(wrapped_lines, max_lines)

    @classmethod
    def _estimate_ass_line_render_width(cls, line, font_size, outline, shadow):
        text_units = cls._estimate_subtitle_text_units(str(line or ''))
        if text_units <= 0:
            return 0.0
        # Padding accounts for the rounded box (BorderStyle=4) or outline,
        # the shadow offset and a small safety margin. The coefficients were
        # calibrated against Source Han Sans HW SC rendered at 1080p.
        padding = max(
            8.0,
            float(outline) * 2.8 + float(shadow) * 1.8 + float(font_size) * 0.14,
        )
        return text_units * float(font_size) + padding

    @classmethod
    def _check_ass_lines_width_safety(cls, lines, usable_width, font_size, outline, shadow):
        safe_width = max(1.0, float(usable_width) * cls._ASS_SAFE_WIDTH_RATIO)
        line_widths = [
            cls._estimate_ass_line_render_width(line, font_size, outline, shadow)
            for line in (lines or [])
            if str(line or '').strip()
        ]
        if not line_widths:
            return True, 0.0, safe_width, []
        max_width = max(line_widths)
        return max_width <= safe_width, max_width, safe_width, line_widths

    @classmethod
    def _estimate_safe_line_units(cls, usable_width, font_size, outline, shadow):
        safe_width = max(1.0, float(usable_width) * cls._ASS_SAFE_WIDTH_RATIO)
        padding = max(6.0, float(outline) * 4.0 + float(shadow) * 2.0 + float(font_size) * 0.08)
        return max(
            float(cls._ASS_HARD_WRAP_MIN_LINE_LENGTH),
            (safe_width - padding) / max(1.0, float(font_size)),
        )

    @classmethod
    def _can_lines_fit_with_font_override(cls, lines, usable_width, font_size, outline, shadow):
        fits, _, _, _ = cls._check_ass_lines_width_safety(
            lines,
            usable_width,
            font_size,
            outline,
            shadow,
        )
        if fits:
            return True

        _, _, _, override_fits = cls._resolve_safe_override_font_size(
            lines,
            usable_width,
            font_size,
            outline,
            shadow,
        )
        return override_fits

    @classmethod
    def _score_partition_line(cls, line, target_units, max_line_length, *, is_last):
        stripped = str(line or '').strip()
        if not stripped:
            return float('inf')

        units = cls._estimate_subtitle_text_units(stripped)
        # Soft length constraint with a generous tolerance: hard-reject only
        # when a line is extremely long, otherwise apply a strong quadratic
        # penalty.  This keeps the DP out of the greedy fallback for portrait
        # cues where perfect boundary-aligned partitions would otherwise be
        # impossible within a tight budget.
        hard_limit = max(float(max_line_length) * 1.5, float(max_line_length) + 8.0)
        if units > hard_limit:
            return float('inf')

        score = (abs(units - float(target_units)) ** 2) * 1.3
        if units > float(max_line_length):
            score += ((units - float(max_line_length)) ** 2) * 36.0

        minimum_units = max(4.5, float(target_units) * (0.60 if is_last else 0.72))
        if units < minimum_units:
            score += (minimum_units - units) ** 2 * (8.0 if is_last else 16.0)

        if stripped[0] in '.,!?;:，。！？；：、)]}】）》」』':
            score += 25.0
        if stripped[-1] in '([{【（《“‘':
            score += 25.0
        if not is_last and cls._is_short_orphan_tail(stripped):
            score += 10.0
        return score

    @classmethod
    def _build_optimal_multiline_partition(
        cls,
        normalized,
        *,
        max_line_length,
        min_lines,
        max_lines,
    ):
        from functools import lru_cache

        raw_segments = [segment.strip() for segment in str(normalized or '').split('\n') if segment.strip()]
        merged_text = cls._merge_subtitle_text_parts(raw_segments)
        if not merged_text:
            return []

        minimum_lines = max(1, int(min_lines))
        maximum_lines = max(minimum_lines, int(max_lines))

        preferred_points = cls._collect_candidate_wrap_indices(merged_text, include_fallback=False)
        all_points = cls._collect_candidate_wrap_indices(merged_text, include_fallback=True)

        total_units = cls._estimate_subtitle_text_units(merged_text)
        best_score = None
        best_lines = []

        for point_source in (preferred_points, all_points):
            candidate_points = point_source
            if not candidate_points:
                continue

            points = [0] + sorted(set(idx for idx in candidate_points if 0 < idx < len(merged_text))) + [len(merged_text)]
            if len(points) < minimum_lines + 1:
                continue

            if len(points) < 2:
                return [merged_text]

            for line_count in range(minimum_lines, maximum_lines + 1):
                target_units = max(6.0, total_units / max(1, line_count))

                @lru_cache(maxsize=None)
                def solve(start_idx, lines_left):
                    remaining_text = merged_text[points[start_idx]:].strip()
                    if not remaining_text:
                        return float('inf'), tuple()

                    if lines_left == 1:
                        line_score = cls._score_partition_line(
                            remaining_text,
                            target_units,
                            max_line_length,
                            is_last=True,
                        )
                        if line_score == float('inf'):
                            return float('inf'), tuple()
                        return line_score, (remaining_text,)

                    best_local = float('inf'), tuple()
                    max_next_index = len(points) - lines_left
                    for next_idx in range(start_idx + 1, max_next_index + 1):
                        line = merged_text[points[start_idx]:points[next_idx]].strip()
                        if not line:
                            continue

                        current_score = cls._score_partition_line(
                            line,
                            target_units,
                            max_line_length,
                            is_last=False,
                        )
                        if current_score == float('inf'):
                            continue

                        tail_score, tail_lines = solve(next_idx, lines_left - 1)
                        total_score = current_score + tail_score
                        if total_score < best_local[0]:
                            best_local = total_score, (line,) + tail_lines

                    return best_local

                score, lines = solve(0, line_count)
                if not lines:
                    continue

                score += max(0, line_count - minimum_lines) * 3.0
                if best_score is None or score < best_score:
                    best_score = score
                    best_lines = list(lines)

            if best_lines:
                break

        return best_lines

    @classmethod
    def _find_portrait_rescue_lines(
        cls,
        normalized,
        *,
        max_line_length,
        usable_width,
        font_size,
        outline,
        shadow,
    ):
        rescue_font_size = max(
            float(cls._ASS_OVERRIDE_FONT_SIZE_MIN),
            float(font_size) * float(cls._ASS_OVERRIDE_FONT_SIZE_RATIO_MIN),
        )
        rescue_outline = cls._clamp(
            rescue_font_size * cls._ASS_OVERRIDE_OUTLINE_RATIO,
            cls._ASS_OVERRIDE_OUTLINE_MIN,
            cls._ASS_OVERRIDE_OUTLINE_MAX,
        )
        rescue_shadow = cls._clamp(rescue_font_size * 0.016, 0.7, 1.35)
        rescue_line_length = int(round(
            cls._estimate_safe_line_units(
                usable_width,
                rescue_font_size,
                rescue_outline,
                rescue_shadow,
            )
        ))
        rescue_line_length = int(cls._clamp(
            rescue_line_length,
            max_line_length + 1,
            cls._ASS_PORTRAIT_RESCUE_LINE_LENGTH_MAX,
        ))

        best_lines = []
        for line_count in (4, 5):
            candidate_lines = cls._build_optimal_multiline_partition(
                normalized,
                max_line_length=rescue_line_length,
                min_lines=line_count,
                max_lines=line_count,
            )
            if not candidate_lines:
                continue

            if cls._can_lines_fit_with_font_override(
                candidate_lines,
                usable_width,
                font_size,
                outline,
                shadow,
            ):
                return candidate_lines, True

            if not best_lines:
                best_lines = candidate_lines

        return best_lines, False

    @classmethod
    def _find_landscape_rescue_lines(
        cls,
        normalized,
        *,
        max_line_length,
        usable_width,
        font_size,
        outline,
        shadow,
        max_lines=2,
    ):
        rescue_line_length = int(round(
            cls._estimate_safe_line_units(
                usable_width,
                font_size,
                outline,
                shadow,
            )
        ))
        rescue_line_length = int(cls._clamp(
            rescue_line_length,
            max_line_length,
            cls._ASS_LANDSCAPE_SINGLE_LINE_LIMIT_MAX,
        ))

        best_lines = []
        line_limit = max(2, int(max_lines or 2))
        for line_count in range(2, line_limit + 1):
            candidate_lines = cls._build_optimal_multiline_partition(
                normalized,
                max_line_length=rescue_line_length,
                min_lines=line_count,
                max_lines=line_count,
            )
            if not candidate_lines:
                continue

            if cls._can_lines_fit_with_font_override(
                candidate_lines,
                usable_width,
                font_size,
                outline,
                shadow,
            ):
                return candidate_lines, True

            if not best_lines:
                best_lines = candidate_lines

        return best_lines, False

    @classmethod
    def _find_safe_hard_wrap_lines(
        cls,
        normalized,
        *,
        max_line_length,
        max_lines,
        usable_width,
        font_size,
        outline,
        shadow,
    ):
        best_lines = []
        best_width = None

        for line_limit in range(
            int(max_line_length),
            int(cls._ASS_HARD_WRAP_MIN_LINE_LENGTH) - 1,
            -1,
        ):
            candidate_lines = cls._build_aggressive_two_line_candidate(
                normalized,
                max_line_length=line_limit,
                max_lines=max_lines,
            )
            fits, max_width, _, _ = cls._check_ass_lines_width_safety(
                candidate_lines,
                usable_width,
                font_size,
                outline,
                shadow,
            )
            if best_width is None or max_width < best_width:
                best_lines = candidate_lines
                best_width = max_width
            if fits:
                return candidate_lines, True

        return best_lines, False

    @classmethod
    def _build_aggressive_two_line_candidate(cls, normalized, *, max_line_length, max_lines):
        raw_segments = [segment.strip() for segment in str(normalized or '').split('\n') if segment.strip()]
        merged_text = cls._merge_subtitle_text_parts(raw_segments)
        if not merged_text:
            return []
        if max_lines <= 1 or cls._estimate_subtitle_text_units(merged_text) <= max_line_length:
            return [merged_text]

        if max_lines > 2:
            return cls._limit_wrapped_lines(
                cls._wrap_subtitle_segment_greedily(merged_text, max_line_length),
                max_lines,
            )

        split_index = cls._find_balanced_wrap_index(merged_text, max_line_length)
        if split_index > 0:
            return cls._limit_wrapped_lines(
                [merged_text[:split_index].strip(), merged_text[split_index:].strip()],
                max_lines,
            )

        return cls._limit_wrapped_lines(
            cls._wrap_subtitle_segment_greedily(merged_text, max_line_length),
            max_lines,
        )

    @classmethod
    def _resolve_safe_override_font_size(cls, lines, usable_width, font_size, outline, shadow):
        fits, max_width, safe_width, _ = cls._check_ass_lines_width_safety(
            lines,
            usable_width,
            font_size,
            outline,
            shadow,
        )
        if fits or max_width <= 0:
            return None, None, None, True

        min_font_size = max(
            float(cls._ASS_OVERRIDE_FONT_SIZE_MIN),
            float(font_size) * float(cls._ASS_OVERRIDE_FONT_SIZE_RATIO_MIN),
        )
        target_font_size = max(
            min_font_size,
            float(font_size) * (safe_width / max_width),
        )
        target_font_size = min(float(font_size), target_font_size)
        target_font_size = float(int(max(1, round(target_font_size))))
        if target_font_size >= float(font_size):
            return None, None, None, False

        # Keep the configured outline/shadow proportions when a cue shrinks.
        # A disabled style component stays disabled in the inline override.
        outline_ratio = float(outline) / max(1.0, float(font_size))
        shadow_ratio = float(shadow) / max(1.0, float(font_size))
        adjusted_outline = (
            0.0
            if float(outline) <= 0.0
            else cls._clamp(
                target_font_size * outline_ratio,
                0.0,
                cls._ASS_OVERRIDE_OUTLINE_MAX,
            )
        )
        adjusted_shadow = (
            0.0
            if float(shadow) <= 0.0
            else cls._clamp(target_font_size * shadow_ratio, 0.0, cls._ASS_SHADOW_MAX)
        )
        adjusted_fits, _, _, _ = cls._check_ass_lines_width_safety(
            lines,
            usable_width,
            target_font_size,
            adjusted_outline,
            adjusted_shadow,
        )
        return target_font_size, adjusted_outline, adjusted_shadow, adjusted_fits

    @classmethod
    def _resolve_single_line_scaled_font(
        cls,
        text,
        usable_width,
        font_size,
        outline,
        shadow,
        *,
        min_scale,
    ):
        """Find the largest font size >= min_scale*font_size that keeps *text* on one line.

        Uses binary search so the scale reduction is as small as possible,
        keeping the subtitle single-line without an aggressive visual shrink.

        Returns (scaled_font_size, scaled_outline, scaled_shadow) if a fit is found,
        otherwise (None, None, None).
        """
        single_line = [str(text or '').strip()]
        if not single_line[0]:
            return None, None, None

        min_allowed_font = max(
            float(cls._ASS_OVERRIDE_FONT_SIZE_MIN),
            float(font_size) * float(cls._ASS_OVERRIDE_FONT_SIZE_RATIO_MIN),
            float(font_size) * float(min_scale),
        )
        if min_allowed_font >= float(font_size):
            return None, None, None

        def _fits_at_size(size):
            outline_ratio = float(outline) / max(1.0, float(font_size))
            shadow_ratio = float(shadow) / max(1.0, float(font_size))
            test_outline = (
                0.0
                if float(outline) <= 0.0
                else cls._clamp(size * outline_ratio, 0.0, cls._ASS_OVERRIDE_OUTLINE_MAX)
            )
            test_shadow = (
                0.0
                if float(shadow) <= 0.0
                else cls._clamp(size * shadow_ratio, 0.0, cls._ASS_SHADOW_MAX)
            )
            fits, _, _, _ = cls._check_ass_lines_width_safety(
                single_line,
                usable_width,
                size,
                test_outline,
                test_shadow,
            )
            return fits

        lo = min_allowed_font
        hi = float(font_size)
        if _fits_at_size(hi):
            return None, None, None
        if not _fits_at_size(lo):
            return None, None, None

        best_size = lo
        for _ in range(12):
            mid = (lo + hi) / 2.0
            if _fits_at_size(mid):
                best_size = mid
                lo = mid
            else:
                hi = mid

        best_size = float(int(round(best_size)))
        best_size = min(best_size, font_size - 1.0)
        if best_size < min_allowed_font:
            return None, None, None

        outline_ratio = float(outline) / max(1.0, float(font_size))
        shadow_ratio = float(shadow) / max(1.0, float(font_size))
        best_outline = (
            0.0
            if float(outline) <= 0.0
            else cls._clamp(best_size * outline_ratio, 0.0, cls._ASS_OVERRIDE_OUTLINE_MAX)
        )
        best_shadow = (
            0.0
            if float(shadow) <= 0.0
            else cls._clamp(best_size * shadow_ratio, 0.0, cls._ASS_SHADOW_MAX)
        )
        return best_size, best_outline, best_shadow

    @staticmethod
    def _is_preferred_wrap_boundary(char):
        if not char:
            return False
        if char.isspace():
            return True
        return char in '.,!?;:，。！？；：、)]}】）》」』'

    @staticmethod
    def _is_latin_word_char(char):
        if not char:
            return False
        return char.isascii() and (char.isalnum() or char in "'#&+_./-")

    @classmethod
    def _is_disallowed_wrap_pair(cls, left_char, right_char):
        if not left_char or not right_char:
            return False
        if cls._is_latin_word_char(left_char) and cls._is_latin_word_char(right_char):
            return True
        if left_char in '([{【（《“‘' or right_char in ')]}】）》」』”’':
            return True
        # Do not start a wrapped line with sentence punctuation. Keeping the
        # punctuation attached to the preceding line also matches libass's
        # visual rhythm for CJK captions.
        if right_char in '.,!?;:，。！？；：、…':
            return True
        if left_char.isdigit() and right_char.isascii() and right_char.isalpha():
            return True
        if cls._is_latin_word_char(left_char) and right_char == '%':
            return True
        return False

    @classmethod
    def _is_cjk_cjk_split(cls, text, split_index):
        """Return True if *split_index* falls between two CJK-like characters.

        Splitting mid-CJK-run is generally undesirable because most CJK
        words are multi-character compounds.  The caller should penalise
        these positions so the wrapper prefers punctuation, spaces and
        script boundaries instead.
        """
        segment = str(text or '')
        if split_index <= 0 or split_index >= len(segment):
            return False
        return (
            cls._is_cjk_like_char(segment[split_index - 1])
            and cls._is_cjk_like_char(segment[split_index])
        )

    @classmethod
    def _should_keep_ascii_phrase_together(cls, text, split_index):
        segment = str(text or '')
        if split_index <= 0 or split_index >= len(segment):
            return False
        if not segment[split_index - 1].isspace():
            return False

        left_end = split_index - 1
        left_start = left_end - 1
        while left_start >= 0 and cls._is_latin_word_char(segment[left_start]):
            left_start -= 1
        left_token = segment[left_start + 1:left_end].strip()

        right_end = split_index
        while right_end < len(segment) and cls._is_latin_word_char(segment[right_end]):
            right_end += 1
        right_token = segment[split_index:right_end].strip()

        if not left_token or not right_token:
            return False

        combined_length = len(left_token) + len(right_token) + 1
        if combined_length <= 15:
            return True
        if left_token.isupper() and len(left_token) <= 4:
            return True
        return False

    @classmethod
    def _collect_candidate_wrap_indices(cls, segment, include_fallback=True):
        preferred = []
        fallback = []
        for idx in range(1, len(segment)):
            left_char = segment[idx - 1]
            right_char = segment[idx]
            if cls._is_disallowed_wrap_pair(left_char, right_char):
                continue
            if left_char.isspace() and cls._should_keep_ascii_phrase_together(segment, idx):
                continue
            right_text = segment[idx:].strip()
            if cls._is_preferred_wrap_boundary(left_char) or cls._semantic_wrap_bonus(right_text) > 0.0:
                preferred.append(idx)
            else:
                fallback.append(idx)
        if not include_fallback:
            return preferred
        return preferred + fallback

    @classmethod
    def _find_wrap_boundary(cls, chars):
        joined_chars = ''.join(chars)
        for idx in range(len(chars) - 1, -1, -1):
            right_char = chars[idx + 1] if idx + 1 < len(chars) else ''
            if chars[idx].isspace() and cls._should_keep_ascii_phrase_together(joined_chars, idx + 1):
                continue
            if cls._is_preferred_wrap_boundary(chars[idx]) and not cls._is_disallowed_wrap_pair(chars[idx], right_char):
                return idx
        return -1

    @staticmethod
    def _estimate_subtitle_char_units(char):
        if not char:
            return 0.0
        if char.isspace():
            return 0.35
        if unicodedata.east_asian_width(char) in {'W', 'F'}:
            if unicodedata.category(char).startswith('P'):
                return 0.7
            return 1.0
        if char.isascii():
            if char.isalnum():
                return 0.6
            return 0.45
        if unicodedata.category(char).startswith('P'):
            return 0.6
        return 0.8

    @classmethod
    def _estimate_subtitle_text_units(cls, text):
        return sum(cls._estimate_subtitle_char_units(char) for char in str(text or ''))

    @staticmethod
    def _is_punctuation_only_text(text):
        stripped = str(text or '').strip()
        if not stripped:
            return False
        return all(
            char.isspace() or unicodedata.category(char).startswith('P')
            for char in stripped
        )

    @classmethod
    def _is_short_orphan_tail(cls, text):
        stripped = str(text or '').strip()
        if not stripped:
            return False
        if cls._is_punctuation_only_text(stripped):
            return True

        tail_core = stripped.rstrip('.,!?;:，。！？；：、… ')
        if not tail_core:
            return True

        tail_units = cls._estimate_subtitle_text_units(tail_core)
        if tail_units <= 3.0:
            return True

        return tail_core in {'吗', '呢', '啊', '吧', '呀', '了', '嘛', '么', '呗', '哇', '哦', '喔'}

    @classmethod
    def _semantic_wrap_bonus(cls, text):
        stripped = str(text or '').strip()
        if not stripped:
            return 0.0

        strong_prefixes = (
            '而不是', '而非', '并不是', '不是', '但是', '不过', '然而', '因此', '所以',
            '因为', '如果', '虽然', '并且', '或者', '还是', '以及', '然后', '兼顾',
            '保持', '避免', '确保', '否则',
            'rather than', 'instead of', 'because', 'however', 'therefore', 'although',
        )
        weak_prefixes = (
            '而', '但', '却', '并', '或',
            'and', 'but', 'or', 'if', 'when', 'while', 'that', 'which',
        )

        lowered = stripped.lower()
        if lowered.startswith(strong_prefixes):
            return 18.0
        if lowered.startswith(weak_prefixes):
            return 8.0
        return 0.0

    @classmethod
    def _is_broken_compound_wrap(cls, left_text, right_text):
        left = str(left_text or '').strip().lower()
        right = str(right_text or '').strip().lower()
        if not left or not right:
            return False

        broken_pairs = (
            ('而', '不是'),
            ('并', '不是'),
            ('rather', 'than'),
            ('instead', 'of'),
        )
        return any(left.endswith(prefix) and right.startswith(suffix) for prefix, suffix in broken_pairs)

    @classmethod
    def _wrap_subtitle_segment_greedily(cls, segment, max_line_length):
        wrapped_lines = []
        chars = []
        current_units = 0.0
        idx = 0

        while idx < len(segment):
            char = segment[idx]
            char_units = cls._estimate_subtitle_char_units(char)
            if chars and current_units + char_units > max_line_length:
                break_at = cls._find_wrap_boundary(chars)
                if break_at >= 0:
                    line_chars = chars[:break_at + 1]
                    remainder = ''.join(chars[break_at + 1:]).strip()
                else:
                    tail_start = len(chars)
                    while tail_start > 0 and cls._is_latin_word_char(chars[tail_start - 1]):
                        tail_start -= 1
                    if 0 < tail_start < len(chars):
                        line_chars = chars[:tail_start]
                        remainder = ''.join(chars[tail_start:]).strip()
                    elif tail_start == 0 and all(cls._is_latin_word_char(c) for c in chars):
                        # The accumulated buffer is one unbreakable Latin token that
                        # already exceeds the line length. Keep it intact so the
                        # downstream overflow guard can scale the font or wrap via
                        # rescue logic, rather than splitting the word mid-character.
                        line_chars = chars
                        remainder = ''
                    else:
                        line_chars = chars
                        remainder = ''

                line = ''.join(line_chars).strip()
                if line:
                    wrapped_lines.append(line)
                chars = list(remainder)
                current_units = cls._estimate_subtitle_text_units(remainder)
                continue

            chars.append(char)
            current_units += char_units
            idx += 1

        line = ''.join(chars).strip()
        if line:
            wrapped_lines.append(line)
        return wrapped_lines

    @classmethod
    def _find_balanced_wrap_index(cls, segment, max_line_length):
        best_index = -1
        best_score = None

        candidate_indices = cls._collect_candidate_wrap_indices(segment)
        if not candidate_indices:
            return -1

        for idx in candidate_indices:
            left = segment[:idx].strip()
            right = segment[idx:].strip()
            if not left or not right:
                continue

            left_units = cls._estimate_subtitle_text_units(left)
            right_units = cls._estimate_subtitle_text_units(right)
            boundary_char = segment[idx - 1]
            overflow = max(0.0, left_units - max_line_length) + max(0.0, right_units - max_line_length)
            score = abs(left_units - right_units) + overflow * 8.0

            if not cls._is_preferred_wrap_boundary(boundary_char):
                score += 4.5
            elif boundary_char.isspace():
                score -= 1.5
            else:
                score -= 4.0
            if cls._is_short_orphan_tail(right):
                score += 15.0
            if left_units <= 3.0:
                score += 8.0
            # Penalise severely unbalanced splits (ratio > 3:1)
            shorter = min(left_units, right_units)
            longer = max(left_units, right_units)
            if shorter > 0 and longer / shorter > 3.0:
                score += 6.0
            score -= cls._semantic_wrap_bonus(right)
            if cls._is_broken_compound_wrap(left, right):
                score += 12.0
            if cls._is_cjk_cjk_split(segment, idx):
                score += 4.0

            if best_score is None or score < best_score:
                best_score = score
                best_index = idx

        return best_index

    @classmethod
    def _wrap_landscape_segment_for_ass(cls, segment, single_line_limit, max_line_length):
        total_units = cls._estimate_subtitle_text_units(segment)
        if total_units <= single_line_limit:
            return [segment]

        tolerance = cls._SINGLE_LINE_TOLERANCE

        split_index = cls._find_balanced_wrap_index(segment, max_line_length)
        if split_index <= 0:
            # No viable balanced split – keep single if within tolerance
            if total_units <= single_line_limit + tolerance:
                return [segment]
            return cls._wrap_subtitle_segment_greedily(segment, max_line_length)

        first_line = segment[:split_index].strip()
        second_line = segment[split_index:].strip()
        if not first_line or not second_line:
            if total_units <= single_line_limit + tolerance:
                return [segment]
            return cls._wrap_subtitle_segment_greedily(segment, max_line_length)

        # Orphan tail: second line is too short to justify a split
        if cls._is_short_orphan_tail(second_line) and total_units <= single_line_limit + tolerance:
            return [segment]

        first_units = cls._estimate_subtitle_text_units(first_line)
        second_units = cls._estimate_subtitle_text_units(second_line)

        # Severely unbalanced split (ratio > 3:1): prefer single line if within tolerance
        shorter = min(first_units, second_units)
        longer = max(first_units, second_units)
        if shorter > 0 and longer / shorter > 3.0 and total_units <= single_line_limit + tolerance:
            return [segment]

        if first_units > max_line_length * 1.35 or second_units > max_line_length * 1.35:
            fallback_lines = cls._wrap_subtitle_segment_greedily(segment, max_line_length)
            if len(fallback_lines) == 2 and not cls._is_short_orphan_tail(fallback_lines[1]):
                return fallback_lines
            if total_units <= single_line_limit + tolerance:
                return [segment]
            return fallback_lines

        return [first_line, second_line]

    @classmethod
    def _wrap_subtitle_text_for_ass(
        cls,
        text,
        video_width,
        video_height,
        return_meta=False,
        *,
        prefer_single_line=True,
        single_line_min_font_scale=None,
        style_overrides=None,
    ):
        # Normalize internal line breaks so that a single SRT cue is always
        # treated as one logical line. The ASS burn-in stage decides whether
        # to scale the font or emit an overflow warning; it never falls back
        # to multi-line wrapping for landscape captions.
        normalized = cls._merge_subtitle_text_parts(
            str(text or '').replace('\r\n', '\n').replace('\r', '\n').split('\n')
        )
        wrap_meta = {
            'forced_wrap': False,
            'font_override': None,
            'overflow_warning': False,
        }
        if not normalized:
            return ('', wrap_meta) if return_meta else ''

        max_line_length, max_lines = cls._estimate_subtitle_layout_limits(
            video_width, video_height, style_overrides
        )
        style = cls._build_streaming_ass_style(video_width, video_height, style_overrides)
        is_portrait = float(style['PlayResY']) > float(style['PlayResX'])
        usable_width = max(
            120.0,
            float(style['PlayResX']) - float(style['MarginL']) - float(style['MarginR']),
        )
        font_size = max(1.0, float(style['FontSize']))
        outline = float(style['Outline'])
        shadow = float(style['Shadow'])
        single_line_limit = int(cls._clamp(
            round(usable_width / font_size * cls._ASS_LANDSCAPE_SINGLE_LINE_DENSITY),
            cls._ASS_LANDSCAPE_SINGLE_LINE_LIMIT_MIN,
            cls._ASS_LANDSCAPE_SINGLE_LINE_LIMIT_MAX,
        ))
        override_outline = None
        override_shadow = None

        # Single-line priority: keep the whole cue on one line if it fits,
        # optionally scaling the font down within the configured range.
        if prefer_single_line:
            min_scale = max(
                cls._ASS_SINGLE_LINE_FONT_SCALE_MIN,
                float(single_line_min_font_scale or cls._ASS_SINGLE_LINE_FONT_SCALE_MIN),
            )
            single_line = [normalized]
            single_fits, _, _, _ = cls._check_ass_lines_width_safety(
                single_line,
                usable_width,
                font_size,
                outline,
                shadow,
            )
            if single_fits:
                wrapped_lines = single_line
            else:
                scaled_font_size, scaled_outline, scaled_shadow = cls._resolve_single_line_scaled_font(
                    normalized,
                    usable_width,
                    font_size,
                    outline,
                    shadow,
                    min_scale=min_scale,
                )
                if scaled_font_size is not None and scaled_font_size < font_size:
                    wrapped_lines = single_line
                    wrap_meta['font_override'] = int(round(scaled_font_size))
                    ass_text = cls._compose_ass_dialogue_text(
                        wrapped_lines,
                        override_font_size=wrap_meta['font_override'],
                        override_outline=scaled_outline,
                        override_shadow=scaled_shadow,
                    )
                    return (ass_text, wrap_meta) if return_meta else ass_text
                wrapped_lines = cls._build_wrapped_lines_for_ass(
                    normalized,
                    is_portrait=is_portrait,
                    max_line_length=max_line_length,
                    max_lines=max_lines,
                    single_line_limit=single_line_limit,
                    aggressive=False,
                )
        else:
            wrapped_lines = cls._build_wrapped_lines_for_ass(
                normalized,
                is_portrait=is_portrait,
                max_line_length=max_line_length,
                max_lines=max_lines,
                single_line_limit=single_line_limit,
                aggressive=False,
            )

        fits, _, _, _ = cls._check_ass_lines_width_safety(
            wrapped_lines,
            usable_width,
            font_size,
            outline,
            shadow,
        )
        candidate_lines = wrapped_lines
        if not fits:
            hard_wrap_lines, hard_wrap_fits = cls._find_safe_hard_wrap_lines(
                normalized,
                max_line_length=max_line_length,
                max_lines=max_lines,
                usable_width=usable_width,
                font_size=font_size,
                outline=outline,
                shadow=shadow,
            )
            if hard_wrap_lines:
                candidate_lines = hard_wrap_lines
                wrap_meta['forced_wrap'] = candidate_lines != wrapped_lines
            fits = hard_wrap_fits

        if not fits and is_portrait and max_lines < 5:
            portrait_rescue_lines, portrait_rescue_fits = cls._find_portrait_rescue_lines(
                normalized,
                max_line_length=max_line_length,
                usable_width=usable_width,
                font_size=font_size,
                outline=outline,
                shadow=shadow,
            )
            if portrait_rescue_lines:
                candidate_lines = portrait_rescue_lines
                wrap_meta['forced_wrap'] = True
                fits = portrait_rescue_fits
            elif not portrait_rescue_lines:
                portrait_fallback_lines, portrait_fallback_fits = cls._find_safe_hard_wrap_lines(
                    normalized,
                    max_line_length=max(max_line_length, cls._ASS_HARD_WRAP_MIN_LINE_LENGTH),
                    max_lines=5,
                    usable_width=usable_width,
                    font_size=font_size,
                    outline=outline,
                    shadow=shadow,
                )
                if portrait_fallback_lines:
                    candidate_lines = portrait_fallback_lines
                    wrap_meta['forced_wrap'] = True
                    fits = portrait_fallback_fits

        # Landscape captions prefer a single line, then use a balanced
        # two-line rescue when the configured minimum font still overflows.
        if not fits and not is_portrait and 1 < max_lines < 4:
            landscape_rescue_lines, landscape_rescue_fits = cls._find_landscape_rescue_lines(
                normalized,
                max_line_length=max_line_length,
                usable_width=usable_width,
                font_size=font_size,
                outline=outline,
                shadow=shadow,
                max_lines=max_lines,
            )
            if landscape_rescue_lines:
                candidate_lines = landscape_rescue_lines
                wrap_meta['forced_wrap'] = True
                fits = landscape_rescue_fits
            elif not landscape_rescue_lines:
                rescue_lines, rescue_fits = cls._find_safe_hard_wrap_lines(
                    normalized,
                    max_line_length=max(max_line_length - 1, cls._ASS_HARD_WRAP_MIN_LINE_LENGTH),
                    max_lines=max_lines,
                    usable_width=usable_width,
                    font_size=font_size,
                    outline=outline,
                    shadow=shadow,
                )
                if rescue_lines:
                    candidate_lines = rescue_lines
                    wrap_meta['forced_wrap'] = True
                    fits = rescue_fits

            if not fits and not landscape_rescue_lines:
                deep_rescue_lines, deep_rescue_fits = cls._find_safe_hard_wrap_lines(
                    normalized,
                    max_line_length=max(max_line_length - 2, cls._ASS_HARD_WRAP_MIN_LINE_LENGTH),
                    max_lines=max_lines,
                    usable_width=usable_width,
                    font_size=font_size,
                    outline=outline,
                    shadow=shadow,
                )
                if deep_rescue_lines:
                    candidate_lines = deep_rescue_lines
                    wrap_meta['forced_wrap'] = True
                    fits = deep_rescue_fits

        override_font_size = None
        if not fits and candidate_lines:
            (
                override_font_size,
                override_outline,
                override_shadow,
                override_fits,
            ) = cls._resolve_safe_override_font_size(
                candidate_lines,
                usable_width,
                font_size,
                outline,
                shadow,
            )
            if override_font_size is not None:
                wrap_meta['font_override'] = int(round(override_font_size))
                fits = override_fits

        if not fits and candidate_lines:
            wrap_meta['overflow_warning'] = True

        ass_text = cls._compose_ass_dialogue_text(
            candidate_lines,
            override_font_size=override_font_size,
            override_outline=override_outline,
            override_shadow=override_shadow,
        )
        return (ass_text, wrap_meta) if return_meta else ass_text

    @classmethod
    def _rebalance_split_cue_durations(cls, cues):
        if not cues:
            return []

        fixed_cues = [dict(cue or {}) for cue in cues]
        minimum_visible = 0.35
        preferred_duration = 0.6

        for idx, cue in enumerate(fixed_cues):
            start = float(cue.get('start', 0.0) or 0.0)
            end = float(cue.get('end', 0.0) or 0.0)
            if end - start >= 0.05:
                continue

            if idx > 0:
                prev = fixed_cues[idx - 1]
                prev_start = float(prev.get('start', 0.0) or 0.0)
                prev_end = float(prev.get('end', 0.0) or 0.0)
                target_start = max(prev_start + minimum_visible, end - preferred_duration)
                if target_start < end:
                    prev['end'] = target_start
                    cue['start'] = target_start
                    cue['end'] = end
                    continue

            if idx + 1 < len(fixed_cues):
                nxt = fixed_cues[idx + 1]
                next_start = float(nxt.get('start', 0.0) or 0.0)
                next_end = float(nxt.get('end', 0.0) or 0.0)
                target_end = min(next_end - minimum_visible, start + preferred_duration)
                if target_end > start:
                    cue['start'] = start
                    cue['end'] = target_end
                    nxt['start'] = target_end

        return fixed_cues

    @classmethod
    def _parse_subtitle_text_to_cues(cls, subtitle_text, video_width=None, video_height=None):
        from y2b.subtitles.srt_transform_engine import SrtTransformConfig, SrtTransformEngine

        # Hard-coded large limits for SRT parsing: never split an incoming SRT
        # cue here. The ASS burn-in stage (_wrap_subtitle_text_for_ass) decides
        # later whether to scale the font or wrap, based on the real video
        # dimensions and the single-line priority settings.
        engine = SrtTransformEngine(
            SrtTransformConfig(
                max_line_length=999,
                max_lines=99,
                normalize_punctuation=False,
                filter_filler_words=False,
            )
        )
        cues = engine.parse_srt(subtitle_text or '')
        if not cues:
            return []
        processed = engine.apply_text_processing(cues)
        return cls._rebalance_split_cue_durations(processed)

    @staticmethod
    def _normalize_font_match_name(font_name):
        return ' '.join(str(font_name or '').strip().split()).casefold()

    @staticmethod
    def _normalize_font_filename(font_name):
        return os.path.basename(str(font_name or '').strip()).casefold()

    @classmethod
    def _iter_bundled_font_paths(cls):
        fonts_dir = get_app_subdir('fonts')
        if not os.path.isdir(fonts_dir):
            return []
        font_paths = []
        for entry in os.listdir(fonts_dir):
            ext = os.path.splitext(entry)[1].lower()
            if ext in cls._BUNDLED_FONT_EXTENSIONS:
                font_paths.append(os.path.join(fonts_dir, entry))
        return sorted(font_paths)

    @classmethod
    def _read_font_display_names(cls, font_path):
        from PIL import ImageFont

        pil_font = ImageFont.truetype(font_path, size=18)
        family_name, style_name = pil_font.getname()
        family_name = str(family_name or '').strip()
        style_name = str(style_name or '').strip()

        candidates = []
        if family_name and style_name:
            candidates.append(f"{family_name} {style_name}")
        if family_name:
            candidates.append(family_name)
        return tuple(dict.fromkeys(name for name in candidates if name))

    @classmethod
    def _find_bundled_font_by_name(cls, font_name, task_logger=None):
        normalized_target = cls._normalize_font_match_name(font_name)
        normalized_filename = cls._normalize_font_filename(font_name)
        if not normalized_target:
            return None

        for font_path in cls._iter_bundled_font_paths():
            if normalized_filename and os.path.basename(font_path).casefold() == normalized_filename:
                try:
                    display_names = cls._read_font_display_names(font_path)
                except Exception as exc:
                    if task_logger:
                        task_logger.warning(f"读取内置字体信息失败，已跳过 {os.path.basename(font_path)}: {exc}")
                    continue
                primary_name = display_names[0] if display_names else os.path.basename(font_path)
                family_name = display_names[1] if len(display_names) > 1 else primary_name
                return {
                    'font_path': font_path,
                    'font_name': primary_name,
                    'font_family': family_name,
                }

            try:
                display_names = cls._read_font_display_names(font_path)
            except Exception as exc:
                if task_logger:
                    task_logger.warning(f"读取内置字体信息失败，已跳过 {os.path.basename(font_path)}: {exc}")
                continue

            for display_name in display_names:
                if cls._normalize_font_match_name(display_name) == normalized_target:
                    primary_name = display_names[0] if display_names else str(font_name).strip()
                    family_name = display_names[1] if len(display_names) > 1 else primary_name
                    return {
                        'font_path': font_path,
                        'font_name': primary_name,
                        'font_family': family_name,
                    }
        return None

    def _resolve_subtitle_font(self, task_logger, temp_fonts_dir):
        config = getattr(self, 'config', {}) or {}
        configured_font_name = str(
            config.get('SUBTITLE_FONT_NAME') or 'NotoSansCJKsc-Regular.otf'
        ).strip()
        if not configured_font_name:
            configured_font_name = 'NotoSansCJKsc-Regular.otf'

        resolved_font = {
            'configured_font_name': configured_font_name,
            'font_name': configured_font_name,
            'font_family': configured_font_name,
            'matched_font_name': None,
            'font_path': None,
            'temp_font_path': None,
        }

        matched_font = self._find_bundled_font_by_name(configured_font_name, task_logger)
        if not matched_font:
            for fallback_path in self._iter_bundled_font_paths():
                try:
                    display_names = self._read_font_display_names(fallback_path)
                except Exception as exc:
                    if task_logger:
                        task_logger.warning(f"读取内置字体信息失败，已跳过 {os.path.basename(fallback_path)}: {exc}")
                    continue
                primary_name = display_names[0] if display_names else os.path.basename(fallback_path)
                family_name = display_names[1] if len(display_names) > 1 else primary_name
                matched_font = {
                    'font_path': fallback_path,
                    'font_name': primary_name,
                    'font_family': family_name,
                }
                if task_logger:
                    task_logger.warning(
                        f"未在内置字体目录中找到匹配字体: {configured_font_name}，已改用内置字体: {primary_name}"
                    )
                break

        if not matched_font:
            if task_logger:
                task_logger.warning(
                    f"未在内置字体目录中找到匹配字体: {configured_font_name}，将继续使用系统字体解析"
                )
            return resolved_font

        resolved_font['font_path'] = matched_font['font_path']
        resolved_font['matched_font_name'] = matched_font['font_name']
        resolved_font['font_family'] = matched_font['font_family']
        try:
            temp_font_path = os.path.join(temp_fonts_dir, os.path.basename(matched_font['font_path']))
            shutil.copy2(matched_font['font_path'], temp_font_path)
            resolved_font['temp_font_path'] = temp_font_path
            if task_logger:
                task_logger.debug(
                    f"已将内置字幕字体复制到临时目录: {os.path.basename(matched_font['font_path'])}"
                )
        except Exception as exc:
            if task_logger:
                task_logger.warning(f"复制内置字幕字体失败: {exc}")

        return resolved_font

    def _resolve_subtitle_style_overrides(self, task_logger=None):
        """解析烧录字幕的外观配置。

        解析失败时返回 None，此时下游会退回历史默认观感，不会中断烧录。
        """
        try:
            overrides = parse_style_overrides(getattr(self, 'config', {}) or {})
        except Exception as exc:
            if task_logger:
                task_logger.warning(f"解析字幕外观配置失败，回退默认观感: {exc}")
            return None
        if task_logger:
            try:
                for issue in diagnose_style(overrides) or ():
                    task_logger.info(f"字幕外观提示: {issue}")
            except Exception:
                pass
        return overrides

    @staticmethod
    def _style_overrides_differ_from_defaults(style_overrides) -> bool:
        """判断用户是否**确实修改过**字幕外观配置（与出厂默认不同）。

        ``parse_style_overrides`` 总会返回一份完整字典（未配置的键填默认值），
        因此不能靠「字典是否为空」判断用户意图。ASS/SSA 输入分支需要这个判据：
        只有用户真的改过外观时才用 force_style 覆盖源样式，
        否则保留创作者样式，避免凭空破坏用户手作的 ASS。
        """
        if not isinstance(style_overrides, dict):
            return False
        try:
            defaults = parse_style_overrides({})
        except Exception:
            return True
        for key, value in style_overrides.items():
            if key not in defaults:
                return True
            if value != defaults[key]:
                return True
        return False

    @classmethod
    def _build_default_ass_document(
        cls,
        cues,
        font_family,
        video_width,
        video_height,
        *,
        prefer_single_line=True,
        single_line_min_font_scale=None,
        style_overrides=None,
    ):
        style, force_style = cls._build_subtitle_style_description(
            font_family,
            video_width,
            video_height,
            style_overrides,
        )
        font_name = force_style['FontName']
        ass_header = (
            "[Script Info]\n"
            "Title: Streaming Subtitle\n"
            "ScriptType: v4.00+\n"
            f"PlayResX: {style['PlayResX']}\n"
            f"PlayResY: {style['PlayResY']}\n"
            "WrapStyle: 0\n"
            "ScaledBorderAndShadow: yes\n"
            "Collisions: Normal\n"
            "\n"
            "[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
            "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding\n"
            "Style: Default,"
            f"{font_name},"
            f"{force_style['FontSize']},"
            f"{style['PrimaryColour']},"
            f"{style['SecondaryColour']},"
            f"{style['OutlineColour']},"
            f"{style['BackColour']},"
            f"{style['Bold']},0,0,0,100,100,0,0,"
            f"{style['BorderStyle']},"
            f"{force_style['Outline']},"
            f"{force_style['Shadow']},"
            f"{force_style['Alignment']},"
            f"{force_style['MarginL']},"
            f"{force_style['MarginR']},"
            f"{force_style['MarginV']},1\n"
            "\n"
            "[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        )

        ass_lines = []
        forced_wrap_count = 0
        font_override_count = 0
        overflow_warning_count = 0
        for cue in cues or []:
            cue_dict = cue if isinstance(cue, dict) else {}
            wrapped_result = cls._wrap_subtitle_text_for_ass(
                cue_dict.get('text', ''),
                video_width,
                video_height,
                return_meta=True,
                prefer_single_line=prefer_single_line,
                single_line_min_font_scale=single_line_min_font_scale,
                style_overrides=style_overrides,
            )
            # `return_meta=True` is expected to return a tuple, but keep a safe fallback
            # to satisfy static analysis and guard unexpected call-path changes.
            if isinstance(wrapped_result, tuple):
                text, wrap_meta = wrapped_result
            else:
                text = wrapped_result
                wrap_meta = {}
            if not text:
                continue
            if wrap_meta.get('forced_wrap'):
                forced_wrap_count += 1
            if wrap_meta.get('font_override'):
                font_override_count += 1
            if wrap_meta.get('overflow_warning'):
                overflow_warning_count += 1
            ass_lines.append(
                "Dialogue: 0,"
                f"{cls._seconds_to_ass_timestamp(cue_dict.get('start', 0.0))},"
                f"{cls._seconds_to_ass_timestamp(cue_dict.get('end', 0.0))},"
                "Default,,0,0,0,,"
                f"{text}"
            )

        body = '\n'.join(ass_lines)
        if body:
            body += '\n'
        if forced_wrap_count or font_override_count or overflow_warning_count:
            logger.debug(
                "ASS overflow guard summary: forced_wrap=%s, font_override=%s, overflow_warning=%s, cues=%s",
                forced_wrap_count,
                font_override_count,
                overflow_warning_count,
                len(ass_lines),
            )
        if overflow_warning_count:
            logger.warning(
                "ASS overflow guard hit minimum per-cue font size but still detected %s potentially risky cue(s)",
                overflow_warning_count,
            )
        return ass_header + body

    def _convert_srt_to_ass(
        self,
        subtitle_path,
        ass_path,
        task_logger,
        *,
        video_width=None,
        video_height=None,
        font_family=None,
        prefer_single_line=True,
        single_line_min_font_scale=None,
        style_overrides=None,
    ):
        """将SRT/VTT字幕转换为带默认流媒体样式的ASS格式。"""
        try:
            source_path = subtitle_path
            subtitle_ext = os.path.splitext(subtitle_path)[1].lower()

            if subtitle_ext == '.vtt':
                converted_srt = self._convert_vtt_to_srt(subtitle_path, task_logger)
                if not converted_srt or not os.path.exists(converted_srt):
                    task_logger.error("VTT转SRT失败，无法生成ASS字幕")
                    return False
                source_path = converted_srt

            with open(source_path, 'r', encoding='utf-8-sig', errors='replace') as subtitle_file:
                subtitle_content = subtitle_file.read()

            cues = self._parse_subtitle_text_to_cues(
                subtitle_content,
                video_width=video_width,
                video_height=video_height,
            )
            if not cues:
                task_logger.error(f"未解析出有效字幕条目，无法生成ASS: {os.path.basename(subtitle_path)}")
                return False

            config = getattr(self, 'config', {}) or {}
            if style_overrides is None:
                # 未显式指定时按当前配置解析，避免任何调用方漏传导致外观配置静默失效。
                style_overrides = self._resolve_subtitle_style_overrides(task_logger)
            ass_content = self._build_default_ass_document(
                cues,
                font_family=font_family,
                video_width=video_width,
                video_height=video_height,
                prefer_single_line=prefer_single_line,
                single_line_min_font_scale=(
                    single_line_min_font_scale
                    if single_line_min_font_scale is not None
                    else config.get('SUBTITLE_SINGLE_LINE_MIN_FONT_SCALE', self._ASS_SINGLE_LINE_FONT_SCALE_MIN)
                ),
                style_overrides=style_overrides,
            )
            with open(ass_path, 'w', encoding='utf-8') as ass_file:
                ass_file.write(ass_content)

            task_logger.info(f"字幕转换为ASS成功: {ass_path}")
            return True

        except Exception as e:
            task_logger.error(f"SRT/VTT转ASS转换失败: {str(e)}")
            return False
