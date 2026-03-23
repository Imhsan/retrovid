#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later

# ------------------------------------------------------------------------------
# Copyright (c) 2026 Imhsan
#
# This software is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This software is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this software. If not, see <https://www.gnu.org/licenses/>.

# ------------------------------------------------------------------------------
import argparse
import logging
import os
import shutil
import subprocess
import sys
import traceback
import numpy

# ------------------------------------------------------------------------------
app = "retrovid"
log = logging.getLogger(app)
ffmpeg = "ffmpeg"

debug = True

# ------------------------------------------------------------------------------
def create_lookup_table(palette_original, palette_custom):
    # allocate 256^3
    lut = numpy.empty((256 ** 3, 3), dtype=numpy.uint8)

    for original_rgb, custom_rgb in zip(palette_original, palette_custom):
        r, g, b = original_rgb

        r_int = int(r)
        g_int = int(g)
        b_int = int(b)

        # 24-bit R*256^2 + G*256 + B
        index = r_int * 65536 + g_int * 256 + b_int

        lut[index] = custom_rgb

    return lut

# ------------------------------------------------------------------------------
def palette_remap(frame_bytes, lut):
    pixels = numpy.frombuffer(frame_bytes, dtype=numpy.uint8).reshape(-1, 3)

    r, g, b = pixels.T # transpose to 3, N

    # convert from (R, G, B) to 24-bit
    indices = r.astype(numpy.uint32) * 65536 + g.astype(numpy.uint32) * 256 + b.astype(numpy.uint32)

    mapped_pixels = lut[indices].tobytes()

    return mapped_pixels

# ------------------------------------------------------------------------------
def process_run(cmd, stdin=None):
    stdin_pipe = subprocess.PIPE if stdin is not None else None

    try:
        process = subprocess.Popen(cmd, stdin=stdin_pipe, stdout=subprocess.PIPE, stderr=None)

    except Exception as err:
        raise RuntimeError(f"failed to start process") from err

    stdout, stderr = process.communicate(stdin)

    if process.returncode != 0:
        raise RuntimeError(f"process failed with exit code {process.returncode}")

    return stdout

# ------------------------------------------------------------------------------
def process_stream(cmd, stdin_pipe=None):
    try:
        process = subprocess.Popen(cmd, stdin=stdin_pipe, stdout=subprocess.PIPE, stderr=None)

    except Exception as err:
        raise RuntimeError(f"failed to start process") from err

    return process

# ------------------------------------------------------------------------------
def process_read(process, size):
    data = bytearray()

    while len(data) < size:
        try:
            chunk = process.stdout.read(size - len(data))

        except Exception as err:
            return None # just return None here

        if not chunk:
            return None

        data.extend(chunk)

    return data

# ------------------------------------------------------------------------------
def process_write(process, data):
    try:
        process.stdin.write(data)
        process.stdin.flush()

    except Exception as err:
        raise

# ------------------------------------------------------------------------------
def process_terminate(process):
    process.terminate()

# ------------------------------------------------------------------------------
def process_close(process):
    if process.stdin:
        process.stdin.close()

# ------------------------------------------------------------------------------
def process_wait(process):
    try:
        process.wait(timeout=5)

    except subprocess.TimeoutExpired:
        process.terminate()

        raise RuntimeError("process timed out and was terminated")

    if process.returncode != 0:
        raise RuntimeError(f"process failed with exit code {process.returncode}")

# ------------------------------------------------------------------------------
def filter_common(args):
    filters = []

    if debug:
        log_level = "info"
    else:
        log_level = "fatal"

    common = [
        "-hide_banner",
        "-loglevel", f"level+{log_level}",
        "-threads", f"{args.threads}",
        "-thread_queue_size", "512"
    ]
    filters.extend(common)

    return filters

# ------------------------------------------------------------------------------
def filter_audio(args):
    filters = []

    if args.enable_audio:
        audio = [
            "-i", f"{args.input_file}",
            "-map", "0:v",
            "-map", "1:a:0?", # ignore if stream is not found
            "-codec:a", "pcm_s16le",
            "-ar", "48000",
            "-ac", "2"
        ]
        filters.extend(audio)

    return filters

# ------------------------------------------------------------------------------
def filter_preprocess(args):
    filters = ["null"] # no op

    filters.append(f"fps={args.fps}")

    if not args.color_mode:
        filters.append("colorchannelmixer=0.2126:0.7152:0.0722:0:0.2126:0.7152:0.0722:0:0.2126:0.7152:0.0722") # from rec. 709

    eq = []
    if args.brightness is not None:
        eq.append(f"brightness={args.brightness}")

    if args.contrast is not None:
        eq.append(f"contrast={args.contrast}")

    if args.gamma is not None:
        eq.append(f"gamma={args.gamma}")

    if eq:
        filters.append("eq=" + ":".join(eq))

    if args.auto_crop:
        filters.append(f"scale=w={args.width}:h={args.height}:force_original_aspect_ratio=increase:flags={args.down_scaler}")
        filters.append(f"crop=w={args.width}:h={args.height}")

    else:
        filters.append(f"scale=w={args.width}:h={args.height}:flags={args.down_scaler}")

    return ",".join(filters)

# ------------------------------------------------------------------------------
def filter_postprocess(args):
    filters = ["null"] # no op

    if args.up_scale_factor is not None:
        filters.append(f"scale=w=in_w*{args.up_scale_factor}:h=in_h*{args.up_scale_factor}:flags={args.up_scaler}")

    return ",".join(filters)

# ------------------------------------------------------------------------------
def run_palette(args):
    common = filter_common(args)
    preprocess = filter_preprocess(args)
    filters = f"{preprocess},palettegen=max_colors={args.max_colors}:reserve_transparent=0" # need the same filters here as in the preprocess

    cmd = [
        ffmpeg,
        *common,
        "-i", f"{args.input_file}",
        "-filter:v", filters,
        "-update", "1",
        "-codec:v", "ppm",
        "-f", "image2pipe",
        "pipe:1"
    ]

    log.debug(f"{app} command: {' '.join(cmd)}")

    palette_bytes = process_run(cmd)

    cmd = [
        ffmpeg,
        *common,
        "-f", "image2pipe",
        "-codec:v", "ppm",
        "-i", "pipe:0",
        "-f", "rawvideo",
        "-pix_fmt", "rgb24",
        "pipe:1"
    ]

    log.debug(f"{app} command: {' '.join(cmd)}")

    raw_bytes = process_run(cmd, palette_bytes)

    pixels = numpy.frombuffer(raw_bytes, dtype=numpy.uint8).reshape(-1, 3)
    palette_array = numpy.unique(pixels, axis=0).astype(numpy.uint8)

    return palette_bytes, palette_array

# ------------------------------------------------------------------------------
def run_preprocess(args, palette):
    common = filter_common(args)
    preprocess = filter_preprocess(args)
    filters = f"[0:v]{preprocess}[v];[v][1:v]paletteuse=dither={args.dither}:bayer_scale={args.bayer_scale}" # bayer_scale does nothing if dither != bayer so this is fine

    cmd = [
        ffmpeg,
        *common,
        "-i", f"{args.input_file}",
        "-f", "image2pipe",
        "-codec:v", "ppm",
        "-i", "pipe:0",
        "-filter_complex", filters,
        "-f", "rawvideo",
        "-pix_fmt", "rgb24",
        "pipe:1"
    ]

    log.debug(f"{app} command: {' '.join(cmd)}")

    process = process_stream(cmd, stdin_pipe=subprocess.PIPE)

    # write the palette first
    process_write(process, palette)
    process_close(process)

    return process

# ------------------------------------------------------------------------------
def run_postprocess(args):
    common = filter_common(args)
    filters = filter_postprocess(args)
    audio = filter_audio(args)

    overwrite = "-n"
    if args.overwrite:
        overwrite = "-y"

    cmd = [
        ffmpeg,
        *common,
        "-f", "rawvideo",
        "-pix_fmt", "rgb24",
        f"{overwrite}",
        "-s", f"{args.width}x{args.height}",
        "-r", f"{args.fps}",
        "-i", "pipe:0",
        *audio,
        "-filter:v", filters,
        "-codec:v", "ffv1",
        "-g", "1",
        "-map_metadata", "-1", # strip metadata, might cause some problems?
        "-map_chapters", "-1",
        "-f", "matroska",
        f"{args.output_file}"
    ]

    log.debug(f"{app} command: {' '.join(cmd)}")

    return process_stream(cmd, stdin_pipe=subprocess.PIPE)

# ------------------------------------------------------------------------------
def pipeline(args):
    palette_bytes, args.palette_original = run_palette(args)

    if args.palette_custom is not None:
        lut = create_lookup_table(args.palette_original, args.palette_custom)

    preprocess_proc = run_preprocess(args, palette_bytes)
    postprocess_proc = run_postprocess(args)

    interrupt = False

    frame_size = args.width * args.height * 3

    try:
        while True:
            try:
                frame_bytes = process_read(preprocess_proc, frame_size)

            except Exception as err:
                interrupt = True

                raise

            if frame_bytes is None:
                break

            if args.palette_custom is not None:
                frame_bytes = palette_remap(frame_bytes, lut)

            try:
                process_write(postprocess_proc, frame_bytes)

            except Exception as err:
                interrupt = True

                process_terminate(preprocess_proc)

                raise

    except KeyboardInterrupt:
        interrupt = True

        process_terminate(preprocess_proc)
        process_terminate(postprocess_proc)

        raise

    finally:
        if not interrupt:
            process_close(postprocess_proc)

            process_wait(preprocess_proc)
            process_wait(postprocess_proc)

        pass

# ------------------------------------------------------------------------------
def validate_input_arg(parser, argument, input_file):
    absolute = os.path.abspath(input_file)

    if not os.path.isfile(absolute):
        parser.error(f"argument {argument}: invalid input: file not found: '{absolute}'")

    if not os.access(absolute, os.R_OK):
        parser.error(f"argument {argument}: invalid input: cannot read from: '{absolute}'")

    return absolute

# ------------------------------------------------------------------------------
def validate_output_arg(parser, argument, output_file, overwrite):
    absolute = os.path.abspath(output_file)
    directory = os.path.dirname(absolute) or "."

    if not output_file.lower().endswith(".mkv"):
        parser.error(f"argument {argument}: invalid output: must end with .mkv: '{output_file}'")

    if not os.path.isdir(directory):
        parser.error(f"argument {argument}: invalid output: directory not found: '{directory}'")

    if not os.access(directory, os.W_OK):
        parser.error(f"argument {argument}: invalid output: cannot write to directory: '{directory}'")

    if os.path.exists(absolute) and not overwrite:
        parser.error(f"argument {argument}: invalid output: file already exists: '{absolute}' (use --overwrite to replace)")

    return absolute

# ------------------------------------------------------------------------------
def validate_int_arg(parser, argument, value, minimum, maximum):
    if value is None: # stoopid
        return None

    value = int(value)

    if not (minimum <= value <= maximum):
        parser.error(f"argument {argument}: invalid value: '{value}' (out of range {minimum}-{maximum})")

    return value

# ------------------------------------------------------------------------------
def validate_float_arg(parser, argument, value, minimum, maximum):
    if value is None:
        return None

    value = float(value)

    if not (minimum <= value <= maximum):
        parser.error(f"argument {argument}: invalid value: '{value}' (out of range {minimum}-{maximum})")

    return value

# ------------------------------------------------------------------------------
def validate_palette_arg(parser, argument, palette, maxcolors, colormode):
    palette = palette.strip()

    if palette == "":
        return None

    if colormode:
        parser.error(f"argument {argument}: palette cannot be used together with --color-mode")

    colors = palette.split(",")

    if len(colors) != maxcolors:
        parser.error(f"argument {argument}: invalid palette: '{palette}' (must contain exactly {maxcolors} hex colors)")

    color_list = []

    for color in colors:
        if not (color.startswith("#") and len(color) == 7):
            parser.error(f"argument {argument}: invalid palette: '{palette}' (invalid format: '{color}')")

        try:
            r = int(color[1:3], 16)
            g = int(color[3:5], 16)
            b = int(color[5:7], 16)

        except ValueError:
            parser.error(f"argument {argument}: invalid palette: '{palette}' (invalid value: '{color}')")

        color_list.append((r, g, b))

    return numpy.array(color_list, dtype=numpy.uint8)

# ------------------------------------------------------------------------------
def validate_scale_arg(parser, argument, scale, width, height, minimum, maximum):
    if scale is None:
        return None

    scale = int(scale)

    if scale < 2:
        parser.error(f"argument {argument}: invalid value: '{scale}' (must be 2 or greater)")

    scaled_width = width * scale
    scaled_height = height * scale

    if not (minimum <= scaled_width <= maximum):
        parser.error(f"argument {argument}: invalid value: '{scale}' (scaled width {scaled_width} is out of range {minimum}-{maximum})")

    if not (minimum <= scaled_height <= maximum):
        parser.error(f"argument {argument}: invalid value: '{scale}' (scaled height {scaled_height} is out of range {minimum}-{maximum})")

    return scale

# ------------------------------------------------------------------------------
def arg_parser():
    parser = argparse.ArgumentParser(prog=app, description=f"convert a video to a color limited, dithered format with an optional custom palette using ffmpeg\ncopyright (c) 2026 imhsan, licensed under the gnu gpl v3\n\nusage: ./%(prog)s.py --input <input> --output <output> [options] [flags]", epilog=f"see https://github.com/imhsan/{app} for more", add_help=False, formatter_class=lambda prog: argparse.RawTextHelpFormatter(prog, max_help_position=30), usage=argparse.SUPPRESS)

    options = parser.add_argument_group("options")
    options.add_argument("--help", action="help", help="show this help message and exit")
    options.add_argument("--input", metavar="<input>", dest="input_file", required=True, help="path to input video (any video format ffmpeg supports)")
    options.add_argument("--output", metavar="<output>", dest="output_file", required=True, help="path to output video ending in .mkv")

    options.add_argument("--width", metavar="<width>", type=int, default=128, help="output width in pixels (default: %(default)s, range: 2-8192)")
    options.add_argument("--height", metavar="<height>", type=int, default=112, help="output height in pixels (default: %(default)s, range: 2-8192)")
    options.add_argument("--fps", metavar="<fps>", type=int, default=10, help="output frames per second (default: %(default)s, range: 1-240)")

    options.add_argument("--brightness", metavar="<brightness>", type=float, default=None, help="adjust brightness (default: 0.0, range: -1.0 to 1.0)")
    options.add_argument("--contrast", metavar="<contrast>", type=float, default=None, help="adjust contrast (default: 1.0, range: -1000.0 to 1000.0)") # this can't be right?
    options.add_argument("--gamma", metavar="<gamma>", type=float, default=None, help="adjust gamma correction (default: 1.0, range: 0.1 to 10.0)")

    options.add_argument("--max-colors", metavar="<maxcolors>", type=int, default=4, help="maximum number of colors in the output (default: %(default)s, range: 1-256)")
    options.add_argument("--palette", metavar="<palette>", default="", help="comma seperated list of colors in #RRGGBB format (default: \"\")")
    options.add_argument("--dither", metavar="<ditherer>", choices=["bayer", "heckbert", "floyd_steinberg", "sierra2", "sierra2_4a", "sierra3", "burkes", "atkinson", "none"], default="bayer", help="dithering algorithm to use (default: %(default)s, choices: %(choices)s)")
    options.add_argument("--bayer-scale", metavar="<factor>", type=int, default=2, help="scale factor for bayer dithering (default: %(default)s, range: 0–5)")

    options.add_argument("--down-scaler", metavar="<scaler>", choices=["fast_bilinear", "bilinear", "bicubic", "neighbor", "area", "bicublin", "gauss", "sinc", "lanczos", "spline"], default="bicubic", help="down scaling algorithm to use (default: %(default)s, choices: %(choices)s)")
    options.add_argument("--up-scaler", metavar="<scaler>", choices=["fast_bilinear", "bilinear", "bicubic", "neighbor", "area", "bicublin", "gauss", "sinc", "lanczos", "spline"], default="neighbor", help="up scaling algorithm to use (default: %(default)s, choices: %(choices)s)")
    options.add_argument("--up-scale-factor", metavar="<factor>", type=int, default=None, help="up scale factor applied to output (default: %(default)s)")

    options.add_argument("--threads", metavar="<threads>", type=int, default=0, help="number of threads to use per ffmpeg process")

    flags = parser.add_argument_group("flags")
    flags.add_argument("--overwrite", default=False, action="store_true", help="overwrite existing output video")
    flags.add_argument("--auto-crop", default=False, action="store_true", help="automatically crop and resize input video to match output video size and aspect ratio")
    flags.add_argument("--enable-audio", default=False, action="store_true", help="copy audio from the input video")
    flags.add_argument("--color-mode", default=False, action="store_true", help="preserve colors instead of converting to grayscale")

    return parser

# ------------------------------------------------------------------------------
def setup():
    if debug:
        log_level = logging.DEBUG
    else:
        log_level = logging.INFO

    logging.basicConfig(level=log_level, format="%(message)s", stream=sys.stderr)

    parser = arg_parser()

    if len(sys.argv) == 1:
        parser.print_help(sys.stderr)
        sys.exit(1)

    args = parser.parse_args()

    args.input_file = validate_input_arg(parser, "--input", args.input_file)
    args.output_file = validate_output_arg(parser, "--output", args.output_file, args.overwrite)

    args.width = validate_int_arg(parser, "--width", args.width, 2, 8192)
    args.height = validate_int_arg(parser, "--height", args.height, 2, 8192)
    args.fps = validate_int_arg(parser, "--fps", args.fps, 1, 240)

    args.brightness = validate_float_arg(parser, "--brightness", args.brightness, -1.0, 1.0)
    args.contrast = validate_float_arg(parser, "--contrast", args.contrast, -1000.0, 1000.0)
    args.gamma = validate_float_arg(parser, "--gamma", args.gamma, 0.1, 10.0)

    args.max_colors = validate_int_arg(parser, "--max-colors", args.max_colors, 1, 256)
    args.palette_custom = validate_palette_arg(parser, "--palette", args.palette, args.max_colors, args.color_mode)
    args.bayer_scale = validate_int_arg(parser, "--bayer-scale", args.bayer_scale, 0, 5)

    args.up_scale_factor = validate_scale_arg(parser, "--up-scale-factor", args.up_scale_factor, args.width, args.height, 2, 8192) # bleht

    args.threads = validate_int_arg(parser, "--threads", args.threads, 0, 2147483647)

    log.debug(f"{app} argparse: {args}")

    ffmpeg_path = shutil.which(ffmpeg)
    if ffmpeg_path is None: # no ffmpeg, no go
        raise RuntimeError(f"{ffmpeg} not found. make sure it is installed and in your path")

    log.debug(f"{app} ffmpeg: {ffmpeg_path}")

    os.environ["AV_LOG_FORCE_NOCOLOR"] = "TRUE" # this should probably not be in here

    return args

# ------------------------------------------------------------------------------
def main():
    args = setup()

    pipeline(args)

# ------------------------------------------------------------------------------
if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        sys.exit(1)

    except Exception as err:
        if len(err.args) >= 2 and isinstance(err.args[1], str):
            msg = err.args[1].lower()

        else:
            msg = str(err).lower()

        if isinstance(err, (RuntimeError, OSError, ValueError)):
            log.error(f"{app} error: {msg}")

        else:
            log.error(f"{app} unexpected error: {msg}")

        if debug:
            traceback.print_exc()

        sys.exit(1)
