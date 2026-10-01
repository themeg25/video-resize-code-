import boto3
import logging
import os
import re
import subprocess
import tempfile
import urllib.parse
import time


# ============================================================
# AWS
# ============================================================

s3 = boto3.client("s3")

logger = logging.getLogger()
logger.setLevel(logging.INFO)


# ============================================================
# CONFIGURATION
# ============================================================

BUCKET_NAME = "stylo-captain-bucket"

INPUT_PREFIX = "compressed-videos/"
BACKUP_PREFIX = "Original-Salon-Images/"

FFMPEG_PATH = "/opt/bin/ffmpeg"

SUPPORTED_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".m4v"
}


# ============================================================
# SIZE CONSTANTS
# ============================================================

KB = 1024
MB = 1024 * KB
GB = 1024 * MB


# ============================================================
# TARGET SIZE RULES
# ============================================================

# < 5 MB
#       Keep unchanged
#
# 5 - 10 MB
#       Target 4 MB
#
# 10 - 25 MB
#       Target 6 MB
#
# 25 - 50 MB
#       Target 10 MB
#
# 50 - 100 MB
#       Target 15 MB
#
# 100 - 200 MB
#       Target 25 MB
#
# 200 - 500 MB
#       Target 40 MB
#
# 500 MB - 1 GB
#       Target 60 MB
#
# > 1 GB
#       Target 100 MB


# ============================================================
# QUALITY SETTINGS
# ============================================================

# Medium keeps Lambda processing practical.
#
# If your Lambda has enough CPU and processing time,
# "slow" can provide slightly better compression efficiency.
#
# Keep medium for large 4K videos to avoid Lambda timeout.

PRESET_X264 = "medium"
PRESET_X265 = "medium"


# ============================================================
# AUDIO
# ============================================================

# Audio bitrate is selected according to target size.


# ============================================================
# S3 LOOP PROTECTION
# ============================================================

PROCESSED_METADATA_KEY = "video-resizer-processed"
PROCESSED_METADATA_VALUE = "true"


# ============================================================
# LAMBDA HANDLER
# ============================================================

def lambda_handler(event, context):

    logger.info("=" * 70)
    logger.info("STYLO VIDEO RESIZER STARTED")
    logger.info("=" * 70)

    logger.info(
        "Bucket: %s",
        BUCKET_NAME
    )

    logger.info(
        "Input folder: %s",
        INPUT_PREFIX
    )

    logger.info(
        "Backup folder: %s",
        BACKUP_PREFIX
    )

    try:

        records = event.get(
            "Records",
            []
        )

        # ====================================================
        # REAL S3 EVENT
        # ====================================================

        if records:

            logger.info(
                "S3 records received: %d",
                len(records)
            )

            results = []

            for record in records:

                try:

                    result = process_s3_record(
                        record
                    )

                    results.append(
                        result
                    )

                except Exception as error:

                    logger.exception(
                        "Failed processing S3 record."
                    )

                    results.append({
                        "status": "failed",
                        "error": str(error)
                    })


        # ====================================================
        # MANUAL TEST
        # ====================================================

        elif (
            event.get("mode") == "test"
            and
            event.get("key")
        ):

            key = urllib.parse.unquote_plus(
                event["key"]
            )

            logger.info(
                "MANUAL TEST"
            )

            logger.info(
                "Key: %s",
                key
            )

            if not key.startswith(
                INPUT_PREFIX
            ):

                raise RuntimeError(
                    "Test key must be inside "
                    "compressed-videos/"
                )

            head = s3.head_object(
                Bucket=BUCKET_NAME,
                Key=key
            )

            result = process_video(
                BUCKET_NAME,
                key,
                head,
                context
            )

            results = [
                result
            ]


        # ====================================================
        # BACKFILL
        # ====================================================

        elif event.get(
            "mode"
        ) == "backfill":

            logger.info(
                "BACKFILL MODE"
            )

            results = process_existing_videos(
                context
            )


        # ====================================================
        # NOTHING
        # ====================================================

        else:

            logger.info(
                "No S3 records received."
            )

            return {
                "statusCode": 200,
                "message": "No S3 records received"
            }


        logger.info("=" * 70)
        logger.info("STYLO VIDEO RESIZER FINISHED")
        logger.info("=" * 70)

        return {
            "statusCode": 200,
            "message": "Video processing completed",
            "results": results
        }


    except Exception as error:

        logger.exception(
            "VIDEO PROCESSING FAILED"
        )

        return {
            "statusCode": 500,
            "message": "Video processing failed",
            "error": str(error)
        }


# ============================================================
# S3 RECORD
# ============================================================

def process_s3_record(
    record
):

    bucket = (
        record[
            "s3"
        ][
            "bucket"
        ][
            "name"
        ]
    )

    encoded_key = (
        record[
            "s3"
        ][
            "object"
        ][
            "key"
        ]
    )

    key = urllib.parse.unquote_plus(
        encoded_key
    )

    logger.info("-" * 70)
    logger.info("S3 OBJECT RECEIVED")
    logger.info("-" * 70)

    logger.info(
        "Bucket: %s",
        bucket
    )

    logger.info(
        "Key: %s",
        key
    )


    # ========================================================
    # BUCKET CHECK
    # ========================================================

    if bucket != BUCKET_NAME:

        return {
            "input": key,
            "status": "ignored",
            "reason": "Unexpected bucket"
        }


    # ========================================================
    # PREFIX CHECK
    # ========================================================

    if not key.startswith(
        INPUT_PREFIX
    ):

        return {
            "input": key,
            "status": "ignored",
            "reason": "Outside compressed-videos/"
        }


    # ========================================================
    # FOLDER CHECK
    # ========================================================

    if key.endswith("/"):

        return {
            "input": key,
            "status": "ignored",
            "reason": "Folder placeholder"
        }


    # ========================================================
    # EXTENSION
    # ========================================================

    extension = os.path.splitext(
        key
    )[1].lower()

    if extension not in SUPPORTED_EXTENSIONS:

        return {
            "input": key,
            "status": "ignored",
            "reason": "Unsupported video extension"
        }


    # ========================================================
    # HEAD
    # ========================================================

    head = s3.head_object(
        Bucket=bucket,
        Key=key
    )

    metadata = {
        str(k).lower(): str(v)
        for k, v in head.get(
            "Metadata",
            {}
        ).items()
    }


    # ========================================================
    # LOOP PROTECTION
    # ========================================================

    if (
        metadata.get(
            PROCESSED_METADATA_KEY
        )
        ==
        PROCESSED_METADATA_VALUE
    ):

        logger.info(
            "Already processed."
        )

        return {
            "input": key,
            "status": "already_processed"
        }


    # ========================================================
    # PROCESS
    # ========================================================

    return process_video(
        bucket,
        key,
        head,
        None
    )


# ============================================================
# PROCESS VIDEO
# ============================================================

def process_video(
    bucket,
    source_key,
    head,
    context=None
):

    original_size = int(
        head["ContentLength"]
    )

    original_mb = (
        original_size / MB
    )

    logger.info("=" * 70)
    logger.info("VIDEO PROCESSING")
    logger.info("=" * 70)

    logger.info(
        "Original file: %s",
        source_key
    )

    logger.info(
        "Original size: %.2f MB",
        original_mb
    )


    # ========================================================
    # TARGET SIZE
    # ========================================================

    target_size = get_target_size(
        original_size
    )


    # ========================================================
    # BACKUP ORIGINAL
    # ========================================================

    relative_key = source_key[
        len(INPUT_PREFIX):
    ]

    backup_key = (
        BACKUP_PREFIX +
        relative_key
    )

    logger.info(
        "Original backup: %s",
        backup_key
    )

    # Do not overwrite an existing backup.
    try:

        s3.head_object(
            Bucket=bucket,
            Key=backup_key
        )

        logger.info(
            "Backup already exists: %s",
            backup_key
        )

    except s3.exceptions.ClientError as error:

        error_code = error.response.get(
            "Error",
            {}
        ).get(
            "Code"
        )

        if error_code in (
            "404",
            "NoSuchKey",
            "NotFound"
        ):

            s3.copy_object(
                Bucket=bucket,
                Key=backup_key,
                CopySource={
                    "Bucket": bucket,
                    "Key": source_key
                }
            )

            logger.info(
                "Original backup completed."
            )

        else:

            raise


    # ========================================================
    # LESS THAN 5 MB
    # ========================================================

    if target_size is None:

        logger.info(
            "Video is below 5 MB."
        )

        logger.info(
            "Keeping original unchanged."
        )

        return {
            "input": source_key,
            "backup": backup_key,
            "output": source_key,
            "status": "unchanged",
            "original_size_mb": round(
                original_mb,
                2
            ),
            "final_size_mb": round(
                original_mb,
                2
            )
        }


    logger.info(
        "Target size: %.2f MB",
        target_size / MB
    )


    # ========================================================
    # CHECK FFMPEG
    # ========================================================

    check_ffmpeg()


    # ========================================================
    # TEMP DIRECTORY
    # ========================================================

    with tempfile.TemporaryDirectory(
        dir="/tmp"
    ) as temp_dir:

        extension = os.path.splitext(
            source_key
        )[1].lower()

        input_path = os.path.join(
            temp_dir,
            "original" + extension
        )

        output_path = os.path.join(
            temp_dir,
            "compressed" + extension
        )


        # ====================================================
        # DOWNLOAD
        # ====================================================

        logger.info(
            "Downloading original video..."
        )

        s3.download_file(
            bucket,
            source_key,
            input_path
        )

        logger.info(
            "Download completed."
        )


        # ====================================================
        # VIDEO INFORMATION
        # ====================================================

        info = get_video_info(
            input_path
        )

        duration = info[
            "duration"
        ]

        width = info[
            "width"
        ]

        height = info[
            "height"
        ]

        fps = info[
            "fps"
        ]

        pixel_format = info[
            "pixel_format"
        ]

        codec = info[
            "codec"
        ]

        color_primaries = info[
            "color_primaries"
        ]

        color_transfer = info[
            "color_transfer"
        ]

        color_space = info[
            "color_space"
        ]

        color_range = info[
            "color_range"
        ]

        is_hdr = info[
            "is_hdr"
        ]

        has_audio = info[
            "has_audio"
        ]


        # ====================================================
        # BIT DEPTH
        # ====================================================

        is_10bit = (
            "10" in pixel_format.lower()
            or
            "12" in pixel_format.lower()
        )


        logger.info(
            "Duration: %.2f seconds",
            duration
        )

        logger.info(
            "Resolution: %sx%s",
            width,
            height
        )

        logger.info(
            "FPS: %.3f",
            fps
        )

        logger.info(
            "Codec: %s",
            codec
        )

        logger.info(
            "Pixel format: %s",
            pixel_format
        )

        logger.info(
            "10-bit/12-bit: %s",
            is_10bit
        )

        logger.info(
            "Color primaries: %s",
            color_primaries
        )

        logger.info(
            "Color transfer: %s",
            color_transfer
        )

        logger.info(
            "Color space: %s",
            color_space
        )

        logger.info(
            "Color range: %s",
            color_range
        )

        logger.info(
            "HDR/HLG source: %s",
            is_hdr
        )

        logger.info(
            "Audio: %s",
            has_audio
        )


        # ====================================================
        # ENCODER
        # ====================================================

        encoder = choose_encoder(
            is_10bit
        )

        logger.info(
            "Selected encoder: %s",
            encoder
        )


        # ====================================================
        # AUDIO BITRATE
        # ====================================================

        audio_bitrate = get_audio_bitrate(
            target_size
        )

        logger.info(
            "Audio bitrate: %d bps",
            audio_bitrate
        )


        # ====================================================
        # INITIAL VIDEO BITRATE
        # ====================================================

        video_bitrate = calculate_video_bitrate(
            target_size,
            duration,
            has_audio,
            audio_bitrate
        )

        logger.info(
            "Initial video bitrate: %d bps",
            video_bitrate
        )


        # ====================================================
        # ENCODE ATTEMPTS
        # ====================================================

        max_attempts = 3

        final_size = None

        for attempt in range(
            1,
            max_attempts + 1
        ):

            logger.info("-" * 70)

            logger.info(
                "ENCODE ATTEMPT %d/%d",
                attempt,
                max_attempts
            )


            # ------------------------------------------------
            # Remove previous output
            # ------------------------------------------------

            if os.path.exists(
                output_path
            ):

                os.remove(
                    output_path
                )


            # ------------------------------------------------
            # Encode
            # ------------------------------------------------

            encode_video(
                input_path=input_path,
                output_path=output_path,
                video_bitrate=video_bitrate,
                audio_bitrate=audio_bitrate,
                encoder=encoder,
                is_10bit=is_10bit,
                has_audio=has_audio,
                color_primaries=color_primaries,
                color_transfer=color_transfer,
                color_space=color_space,
                color_range=color_range,
                is_hdr=is_hdr
            )


            # ------------------------------------------------
            # Check output
            # ------------------------------------------------

            final_size = os.path.getsize(
                output_path
            )

            logger.info(
                "Encode result: %.2f MB",
                final_size / MB
            )


            # ------------------------------------------------
            # Target reached
            # ------------------------------------------------

            if final_size <= target_size:

                logger.info(
                    "TARGET SIZE REACHED."
                )

                break


            # ------------------------------------------------
            # Calculate new bitrate
            # ------------------------------------------------

            ratio = (
                target_size /
                final_size
            )

            adjusted_bitrate = int(
                video_bitrate *
                ratio *
                0.94
            )

            adjusted_bitrate = max(
                adjusted_bitrate,
                80_000
            )

            logger.info(
                "Output is larger than target."
            )

            logger.info(
                "New video bitrate: %d bps",
                adjusted_bitrate
            )

            video_bitrate = (
                adjusted_bitrate
            )


        # ====================================================
        # CHECK OUTPUT
        # ====================================================

        if final_size is None:

            raise RuntimeError(
                "No compressed output was created."
            )


        # ====================================================
        # TARGET NOT REACHED
        # ====================================================

        if final_size > target_size:

            logger.warning(
                "Target size was not reached."
            )

            logger.warning(
                "Target: %.2f MB",
                target_size / MB
            )

            logger.warning(
                "Final: %.2f MB",
                final_size / MB
            )

            return {
                "input": source_key,
                "backup": backup_key,
                "output": source_key,
                "status": "original_kept",
                "reason": "Target size could not be reached",
                "original_size_mb": round(
                    original_mb,
                    2
                ),
                "target_size_mb": round(
                    target_size / MB,
                    2
                ),
                "compressed_size_mb": round(
                    final_size / MB,
                    2
                )
            }


        # ====================================================
        # NEVER MAKE FILE LARGER
        # ====================================================

        if final_size >= original_size:

            logger.warning(
                "Compressed file is not smaller."
            )

            return {
                "input": source_key,
                "backup": backup_key,
                "output": source_key,
                "status": "original_kept",
                "reason": "Compressed file was not smaller",
                "original_size_mb": round(
                    original_mb,
                    2
                ),
                "compressed_size_mb": round(
                    final_size / MB,
                    2
                )
            }


        # ====================================================
        # CONTENT TYPE
        # ====================================================

        extension = os.path.splitext(
            source_key
        )[1].lower()

        content_type = get_content_type(
            extension
        )


        # ====================================================
        # METADATA
        # ====================================================

        new_metadata = {}

        for key, value in head.get(
            "Metadata",
            {}
        ).items():

            new_metadata[
                str(key)
            ] = str(value)


        new_metadata[
            PROCESSED_METADATA_KEY
        ] = PROCESSED_METADATA_VALUE

        new_metadata[
            "original-size-bytes"
        ] = str(
            original_size
        )

        new_metadata[
            "target-size-bytes"
        ] = str(
            target_size
        )

        new_metadata[
            "final-size-bytes"
        ] = str(
            final_size
        )

        new_metadata[
            "original-width"
        ] = str(
            width
        )

        new_metadata[
            "original-height"
        ] = str(
            height
        )

        new_metadata[
            "original-fps"
        ] = str(
            fps
        )

        new_metadata[
            "original-codec"
        ] = str(
            codec
        )

        new_metadata[
            "encoder"
        ] = str(
            encoder
        )

        new_metadata[
            "hdr-source"
        ] = str(
            is_hdr
        )

        new_metadata[
            "output-color"
        ] = (
            "bt709-sdr"
            if is_hdr
            else
            "source-sdr"
        )


        # ====================================================
        # UPLOAD
        # ====================================================

        logger.info(
            "Uploading compressed video..."
        )

        s3.upload_file(
            output_path,
            bucket,
            source_key,
            ExtraArgs={
                "ContentType": content_type,
                "CacheControl": "max-age=31536000",
                "Metadata": new_metadata
            }
        )


        final_mb = (
            final_size / MB
        )

        compression_percent = (
            1 -
            (
                final_size /
                original_size
            )
        ) * 100


        # ====================================================
        # SUCCESS LOG
        # ====================================================

        logger.info("=" * 70)
        logger.info(
            "VIDEO PROCESSING COMPLETED"
        )
        logger.info("=" * 70)

        logger.info(
            "Original: %.2f MB",
            original_mb
        )

        logger.info(
            "Target: %.2f MB",
            target_size / MB
        )

        logger.info(
            "Final: %.2f MB",
            final_mb
        )

        logger.info(
            "Compression: %.2f%%",
            compression_percent
        )

        logger.info(
            "Resolution preserved: %sx%s",
            width,
            height
        )

        logger.info(
            "FPS preserved: %.3f",
            fps
        )

        logger.info(
            "Audio preserved: %s",
            has_audio
        )

        logger.info(
            "Encoder: %s",
            encoder
        )

        logger.info(
            "Source pixel format: %s",
            pixel_format
        )

        logger.info(
            "Source color primaries: %s",
            color_primaries
        )

        logger.info(
            "Source color transfer: %s",
            color_transfer
        )

        logger.info(
            "Source color space: %s",
            color_space
        )

        logger.info(
            "Source color range: %s",
            color_range
        )

        logger.info(
            "HDR -> SDR: %s",
            is_hdr
        )


        return {
            "input": source_key,
            "backup": backup_key,
            "output": source_key,
            "status": "processed",
            "original_size_mb": round(
                original_mb,
                2
            ),
            "target_size_mb": round(
                target_size / MB,
                2
            ),
            "final_size_mb": round(
                final_mb,
                2
            ),
            "compression_percent": round(
                compression_percent,
                2
            ),
            "duration_seconds": round(
                duration,
                2
            ),
            "width": width,
            "height": height,
            "fps": round(
                fps,
                3
            ),
            "codec": codec,
            "encoder": encoder,
            "pixel_format": pixel_format,
            "audio": has_audio,
            "10bit": is_10bit,
            "color_primaries": color_primaries,
            "color_transfer": color_transfer,
            "color_space": color_space,
            "color_range": color_range,
            "hdr_source": is_hdr,
            "output_color": (
                "bt709-sdr"
                if is_hdr
                else
                "source-sdr"
            )
        }


# ============================================================
# TARGET SIZE
# ============================================================

def get_target_size(
    original_size
):

    # ========================================================
    # BELOW 5 MB
    # ========================================================

    if original_size < 5 * MB:

        return None


    # ========================================================
    # 5 - 10 MB
    # ========================================================

    if original_size <= 10 * MB:

        return 4 * MB


    # ========================================================
    # 10 - 25 MB
    # ========================================================

    if original_size <= 25 * MB:

        return 6 * MB


    # ========================================================
    # 25 - 50 MB
    # ========================================================

    if original_size <= 50 * MB:

        return 10 * MB


    # ========================================================
    # 50 - 100 MB
    # ========================================================

    if original_size <= 100 * MB:

        return 15 * MB


    # ========================================================
    # 100 - 200 MB
    # ========================================================

    if original_size <= 200 * MB:

        return 25 * MB


    # ========================================================
    # 200 - 500 MB
    # ========================================================

    if original_size <= 500 * MB:

        return 40 * MB


    # ========================================================
    # 500 MB - 1 GB
    # ========================================================

    if original_size <= 1 * GB:

        return 60 * MB


    # ========================================================
    # ABOVE 1 GB
    # ========================================================

    return 100 * MB


# ============================================================
# FFMPEG CHECK
# ============================================================

def check_ffmpeg():

    if not os.path.isfile(
        FFMPEG_PATH
    ):

        raise RuntimeError(
            "FFmpeg not found at "
            + FFMPEG_PATH
        )

    result = subprocess.run(
        [
            FFMPEG_PATH,
            "-version"
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10
    )

    if result.returncode != 0:

        raise RuntimeError(
            "FFmpeg is not executable."
        )

    logger.info(
        "FFmpeg available."
    )


# ============================================================
# AVAILABLE ENCODERS
# ============================================================

def get_available_encoders():

    result = subprocess.run(
        [
            FFMPEG_PATH,
            "-hide_banner",
            "-encoders"
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30
    )

    output = (
        result.stdout +
        result.stderr
    )

    return output


# ============================================================
# CHOOSE ENCODER
# ============================================================

def choose_encoder(
    is_10bit
):

    encoders = get_available_encoders()


    # Prefer HEVC because it provides better compression
    # efficiency than H.264 at similar visual quality.

    if "libx265" in encoders:

        return "libx265"


    if is_10bit:

        raise RuntimeError(
            "10-bit source detected but "
            "libx265 is not available."
        )


    if "libx264" in encoders:

        return "libx264"


    raise RuntimeError(
        "Neither libx265 nor libx264 is available."
    )


# ============================================================
# VIDEO INFORMATION
# ============================================================

def get_video_info(
    input_path
):

    result = subprocess.run(
        [
            FFMPEG_PATH,
            "-hide_banner",
            "-i",
            input_path
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=120
    )

    output = (
        result.stdout +
        result.stderr
    )


    # ========================================================
    # DURATION
    # ========================================================

    duration_match = re.search(
        r"Duration:\s*(\d+):(\d+):([\d.]+)",
        output
    )

    if not duration_match:

        raise RuntimeError(
            "Could not determine video duration."
        )

    hours = int(
        duration_match.group(1)
    )

    minutes = int(
        duration_match.group(2)
    )

    seconds = float(
        duration_match.group(3)
    )

    duration = (
        hours * 3600
        +
        minutes * 60
        +
        seconds
    )

    if duration <= 0:

        raise RuntimeError(
            "Invalid video duration."
        )


    # ========================================================
    # VIDEO STREAM
    # ========================================================

    video_line = None

    for line in output.splitlines():

        if "Video:" in line:

            video_line = line

            break


    if not video_line:

        raise RuntimeError(
            "Could not find video stream."
        )


    # ========================================================
    # RESOLUTION
    # ========================================================

    resolution_match = re.search(
        r"(\d{2,5})x(\d{2,5})",
        video_line
    )

    if not resolution_match:

        raise RuntimeError(
            "Could not determine video resolution."
        )

    width = int(
        resolution_match.group(1)
    )

    height = int(
        resolution_match.group(2)
    )


    # ========================================================
    # FPS
    # ========================================================

    fps_match = re.search(
        r"(\d+(?:\.\d+)?)\s*(?:fps|tbr)",
        video_line
    )

    if fps_match:

        fps = float(
            fps_match.group(1)
        )

    else:

        fps = 30.0


    # ========================================================
    # CODEC
    # ========================================================

    codec = "unknown"

    codec_match = re.search(
        r"Video:\s*([^,\s]+)",
        video_line
    )

    if codec_match:

        codec = codec_match.group(1)


    # ========================================================
    # PIXEL FORMAT
    # ========================================================

    pixel_format = "unknown"

    pixel_formats = [
        "yuv420p12le",
        "yuv422p12le",
        "yuv444p12le",
        "yuv420p10le",
        "yuv422p10le",
        "yuv444p10le",
        "yuv420p",
        "yuv422p",
        "yuv444p"
    ]

    for fmt in pixel_formats:

        if fmt in video_line:

            pixel_format = fmt

            break


    # ========================================================
    # COLOR
    # ========================================================

    color_primaries = parse_color_value(
        video_line,
        "color primaries"
    )

    color_transfer = parse_color_value(
        video_line,
        "color transfer"
    )

    color_space = parse_color_value(
        video_line,
        "color space"
    )

    color_range = parse_color_range(
        video_line
    )


    # ========================================================
    # AUDIO
    # ========================================================

    has_audio = bool(
        re.search(
            r"\bAudio:",
            output,
            re.IGNORECASE
        )
    )


    # ========================================================
    # HDR DETECTION
    # ========================================================

    is_hdr = is_hdr_source(
        color_primaries,
        color_transfer
    )


    return {
        "duration": duration,
        "width": width,
        "height": height,
        "fps": fps,
        "codec": codec,
        "pixel_format": pixel_format,
        "color_primaries": color_primaries,
        "color_transfer": color_transfer,
        "color_space": color_space,
        "color_range": color_range,
        "has_audio": has_audio,
        "is_hdr": is_hdr
    }


# ============================================================
# HDR / HLG DETECTION
# ============================================================

def is_hdr_source(
    color_primaries,
    color_transfer
):

    transfer = (
        color_transfer or ""
    ).lower()

    primaries = (
        color_primaries or ""
    ).lower()


    # HLG
    if transfer == "arib-std-b67":

        return True


    # HDR10 / PQ
    if transfer in {
        "smpte2084",
        "smpte-st-2084"
    }:

        return True


    # BT.2020 with non-SDR transfer
    if (
        primaries == "bt2020"
        and
        transfer not in {
            "",
            "bt709",
            "unknown"
        }
    ):

        return True


    return False


# ============================================================
# PARSE COLOR VALUE
# ============================================================

def parse_color_value(
    text,
    label
):

    pattern = (
        re.escape(label)
        +
        r"\s*:\s*([a-zA-Z0-9_-]+)"
    )

    match = re.search(
        pattern,
        text,
        re.IGNORECASE
    )

    if match:

        return match.group(1)

    return None


# ============================================================
# PARSE COLOR RANGE
# ============================================================

def parse_color_range(
    text
):

    match = re.search(
        r"color range\s*:\s*([a-zA-Z0-9_]+)",
        text,
        re.IGNORECASE
    )

    if match:

        value = match.group(1).lower()

        if value in {
            "tv",
            "mpeg",
            "limited"
        }:

            return "tv"

        if value in {
            "pc",
            "jpeg",
            "full"
        }:

            return "pc"

    return None


# ============================================================
# AUDIO BITRATE
# ============================================================

def get_audio_bitrate(
    target_size
):

    # Small target
    if target_size <= 6 * MB:

        return 96_000


    # Medium target
    if target_size <= 15 * MB:

        return 128_000


    # Larger target
    return 160_000


# ============================================================
# VIDEO BITRATE
# ============================================================

def calculate_video_bitrate(
    target_size,
    duration,
    has_audio,
    audio_bitrate
):

    # ========================================================
    # Reserve 90% of target
    # ========================================================

    usable_size = (
        target_size *
        0.90
    )

    total_bitrate = (
        usable_size *
        8
    ) / duration


    if has_audio:

        video_bitrate = (
            total_bitrate
            -
            audio_bitrate
        )

    else:

        video_bitrate = (
            total_bitrate
        )


    video_bitrate = max(
        int(video_bitrate),
        80_000
    )

    return video_bitrate


# ============================================================
# ENCODE VIDEO
# ============================================================

def encode_video(
    input_path,
    output_path,
    video_bitrate,
    audio_bitrate,
    encoder,
    is_10bit,
    has_audio,
    color_primaries,
    color_transfer,
    color_space,
    color_range,
    is_hdr
):

    if os.path.exists(
        output_path
    ):

        os.remove(
            output_path
        )


    command = [

        FFMPEG_PATH,

        "-hide_banner",

        "-y",

        "-i",
        input_path,


        # ====================================================
        # VIDEO
        # ====================================================

        "-map",
        "0:v:0",

        "-c:v",
        encoder,


        # ====================================================
        # PRESET
        # ====================================================

        "-preset",

        (
            PRESET_X265
            if encoder == "libx265"
            else PRESET_X264
        ),


        # ====================================================
        # BITRATE
        # ====================================================

        "-b:v",
        str(
            video_bitrate
        ),

        "-maxrate",
        str(
            int(
                video_bitrate *
                1.10
            )
        ),

        "-bufsize",
        str(
            int(
                video_bitrate *
                2.5
            )
        ),


        # ====================================================
        # KEYFRAME
        # ====================================================

        "-g",
        "60",

        "-keyint_min",
        "30"
    ]


    # ========================================================
    # X265 QUALITY SETTINGS
    # ========================================================

    if encoder == "libx265":

        command.extend([
            "-x265-params",
            (
                "aq-mode=3:"
                "aq-strength=0.9:"
                "psy-rd=2.0:"
                "psy-rdoq=1.0"
            )
        ])


    # ========================================================
    # HDR / HLG -> SDR BT.709
    # ========================================================

    if is_hdr:

        video_filter = (
            "zscale="
            "primaries=bt2020:"
            "transfer=linear:"
            "matrix=bt2020nc:"
            "range=tv:"
            "npl=100,"
            "format=gbrpf32le,"
            "tonemap="
            "tonemap=mobius:"
            "param=0.3:"
            "desat=0,"
            "zscale="
            "primaries=bt709:"
            "transfer=bt709:"
            "matrix=bt709:"
            "range=tv,"
            "format=yuv420p"
        )


        command.extend([

            "-vf",
            video_filter,

            "-pix_fmt",
            "yuv420p",

            "-profile:v",
            "main",

            "-color_primaries",
            "bt709",

            "-color_trc",
            "bt709",

            "-colorspace",
            "bt709",

            "-color_range",
            "tv",

            # Remove HDR/Dolby-Vision metadata from
            # the SDR output.
            "-map_metadata",
            "-1"
        ])


    else:

        # ====================================================
        # NORMAL SDR
        # ====================================================

        if (
            is_10bit
            and
            encoder == "libx265"
        ):

            command.extend([

                "-pix_fmt",
                "yuv420p10le",

                "-profile:v",
                "main10"
            ])

        else:

            command.extend([

                "-pix_fmt",
                "yuv420p"
            ])


            if encoder == "libx264":

                command.extend([

                    "-profile:v",
                    "high"
                ])

            else:

                command.extend([

                    "-profile:v",
                    "main"
                ])


        # Preserve metadata for normal SDR video.
        command.extend([

            "-map_metadata",
            "0"
        ])


        if color_primaries:

            command.extend([

                "-color_primaries",
                color_primaries
            ])


        if color_transfer:

            command.extend([

                "-color_trc",
                color_transfer
            ])


        if color_space:

            command.extend([

                "-colorspace",
                color_space
            ])


        if color_range:

            command.extend([

                "-color_range",
                color_range
            ])


    # ========================================================
    # AUDIO
    # ========================================================

    if has_audio:

        command.extend([

            "-map",
            "0:a:0?",

            "-c:a",
            "aac",

            "-b:a",
            str(
                audio_bitrate
            ),

            "-ar",
            "48000",

            "-ac",
            "2"
        ])

    else:

        command.append(
            "-an"
        )


    # ========================================================
    # FAST START
    # ========================================================

    command.extend([

        "-movflags",
        "+faststart"
    ])


    # ========================================================
    # HEVC TAG
    # ========================================================

    if encoder == "libx265":

        command.extend([

            "-tag:v",
            "hvc1"
        ])


    # ========================================================
    # OUTPUT
    # ========================================================

    command.append(
        output_path
    )


    logger.info(
        "Running FFmpeg encode..."
    )

    logger.info(
        "Encoder: %s",
        encoder
    )

    logger.info(
        "Video bitrate: %d bps",
        video_bitrate
    )

    logger.info(
        "Audio bitrate: %d bps",
        audio_bitrate
    )

    logger.info(
        "10-bit source: %s",
        is_10bit
    )

    logger.info(
        "HDR -> SDR: %s",
        is_hdr
    )


    run_ffmpeg(
        command
    )


# ============================================================
# RUN FFMPEG
# ============================================================

def run_ffmpeg(
    command
):

    start_time = time.time()


    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,

        # FFmpeg maximum operation time.
        timeout=840
    )


    elapsed = (
        time.time()
        -
        start_time
    )


    logger.info(
        "FFmpeg runtime: %.2f seconds",
        elapsed
    )


    if result.returncode != 0:

        logger.error(
            "FFmpeg ERROR:"
        )

        logger.error(
            result.stderr[-12000:]
        )

        raise RuntimeError(
            "FFmpeg encoding failed:\n"
            +
            result.stderr[-12000:]
        )


# ============================================================
# CONTENT TYPE
# ============================================================

def get_content_type(
    extension
):

    if extension == ".mp4":

        return "video/mp4"


    if extension == ".mov":

        return "video/quicktime"


    if extension == ".m4v":

        return "video/x-m4v"


    return "application/octet-stream"


# ============================================================
# BACKFILL EXISTING VIDEOS
# ============================================================

def process_existing_videos(
    context=None
):

    logger.info(
        "Existing video backfill started."
    )

    results = []


    paginator = s3.get_paginator(
        "list_objects_v2"
    )


    for page in paginator.paginate(
        Bucket=BUCKET_NAME,
        Prefix=INPUT_PREFIX
    ):

        for obj in page.get(
            "Contents",
            []
        ):

            key = obj["Key"]


            if key.endswith("/"):

                continue


            extension = os.path.splitext(
                key
            )[1].lower()


            if extension not in (
                SUPPORTED_EXTENSIONS
            ):

                continue


            try:

                head = s3.head_object(
                    Bucket=BUCKET_NAME,
                    Key=key
                )


                metadata = {
                    str(k).lower(): str(v)
                    for k, v in head.get(
                        "Metadata",
                        {}
                    ).items()
                }


                if (
                    metadata.get(
                        PROCESSED_METADATA_KEY
                    )
                    ==
                    PROCESSED_METADATA_VALUE
                ):

                    logger.info(
                        "Already processed: %s",
                        key
                    )

                    continue


                result = process_video(
                    BUCKET_NAME,
                    key,
                    head,
                    context
                )


                results.append(
                    result
                )


            except Exception as error:

                logger.exception(
                    "Failed processing: %s",
                    key
                )


                results.append({

                    "input": key,

                    "status": "failed",

                    "error": str(error)

                })


    return results