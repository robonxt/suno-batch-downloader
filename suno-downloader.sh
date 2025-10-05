#!/bin/bash

set -e

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --songfile)
            SONGFILE="$2"
            shift 2
            ;;
        --include-mp4)
            INCLUDE_MP4=true
            shift
            ;;
        *)
            echo "Unknown parameter passed: $1"
            echo "Usage: ./downloader.sh --songfile <filename> [--include-mp4]"
            exit 1
            ;;
    esac
done

# Ensure SONGFILE is provided
if [[ -z "$SONGFILE" ]]; then
    echo "Error: --songfile parameter is required."
    echo "Usage: ./downloader.sh --songfile <filename> [--include-mp4]"
    exit 1
fi

# Check if file exists
if [[ ! -f "$SONGFILE" ]]; then
    echo "Error: File '$SONGFILE' does not exist."
    exit 1
fi

# Derive folder name from songfile (e.g., songs.txt -> songs_files)
BASENAME="${SONGFILE%.*}"
DEST_DIR="${BASENAME}_files"

# Create destination folder if it doesn't exist
mkdir -p "$DEST_DIR"

# Read lines and download audio/video files and their images
# Track downloaded URLs to avoid duplicates
declare -A DOWNLOADED_URLS

while IFS='|' read -r FILENAME URL; do
    URL=$(echo "$URL" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')

    # Check if we should process this file type
    if [[ "$FILENAME" == *.mp3 ]] || { [[ "$INCLUDE_MP4" == true ]] && [[ "$FILENAME" == *.mp4 ]]; }; then
        # Extract UUID from song URL (format: https://cdn1.suno.ai/{UUID}.mp3 or .mp4)
        UUID=$(echo "$URL" | sed 's|https://cdn1\.suno\.ai/||' | sed 's|\.mp[34]||')

        if [[ -z "$UUID" ]]; then
            echo "Warning: Could not extract UUID from $URL"
            continue
        fi

        DEST_FILE="${DEST_DIR}/${FILENAME}"

        # Skip if file already exists on disk
        if [[ -f "$DEST_FILE" ]]; then
            echo "Skipping $FILENAME (already exists)"
            continue
        fi

        # Skip if URL was already downloaded (handles duplicates in input file)
        if [[ ${DOWNLOADED_URLS[$URL]} ]]; then
            echo "Skipping $FILENAME (duplicate URL already downloaded)"
            continue
        fi

        echo "Downloading $FILENAME..."
        echo "URL: '$URL'"
        echo "UUID: '$UUID'"

        # Download the file
        if curl -sSL "$URL" -o "$DEST_FILE"; then
            DOWNLOADED_URLS[$URL]=1
            echo "Successfully downloaded: $(basename "$DEST_FILE")"

            # Embed album art for MP3 files only
            if [[ "$FILENAME" == *.mp3 ]]; then
                # Download associated image
                IMAGE_URL="https://cdn2.suno.ai/image_large_${UUID}.jpeg"
                IMAGE_FILE="${DEST_DIR}/${UUID}.jpeg"

                if [[ ! -f "$IMAGE_FILE" ]]; then
                    echo "Downloading image: ${UUID}.jpeg"
                    if curl -sSL "$IMAGE_URL" -o "$IMAGE_FILE"; then
                        echo "Successfully downloaded image: $(basename "$IMAGE_FILE")"

                        # Embed image as album art in the MP3
                        FINAL_FILE="${DEST_DIR}/${FILENAME}"
                        TEMP_FILE="${DEST_DIR}/temp_${FILENAME}"

                        echo "Embedding album art in ${FILENAME}..."
                        if ffmpeg -i "$DEST_FILE" -i "$IMAGE_FILE" -c copy -map 0 -map 1 -c:v copy -metadata:s:v title="Album cover" -metadata:s:v comment="Cover (front)" -disposition:v attached_pic -y "$TEMP_FILE" 2>/dev/null; then
                            echo "Successfully created temp file with embedded art"
                            # Verify temp file exists and has content
                            if [[ -f "$TEMP_FILE" ]]; then
                                echo "Temp file exists, size: $(stat -f%z "$TEMP_FILE" 2>/dev/null || stat -c%s "$TEMP_FILE" 2>/dev/null || echo 'unknown') bytes"
                                echo "Moving temp file to final location..."
                                if mv "$TEMP_FILE" "$FINAL_FILE"; then
                                    echo "Successfully moved temp file to: $FINAL_FILE"
                                    # Verify final file exists
                                    if [[ -f "$FINAL_FILE" ]]; then
                                        echo "Final file exists, size: $(stat -f%z "$FINAL_FILE" 2>/dev/null || stat -c%s "$FINAL_FILE" 2>/dev/null || echo 'unknown') bytes"
                                        # DON'T remove DEST_FILE since it's the same as FINAL_FILE now!
                                        echo "File ready with embedded album art"
                                    else
                                        echo "ERROR: Final file does not exist after move!"
                                        # Try to recover temp file if final file is missing
                                        if [[ -f "$TEMP_FILE" ]]; then
                                            echo "Attempting to recover temp file..."
                                            mv "$TEMP_FILE" "$FINAL_FILE" || echo "Failed to recover temp file"
                                        fi
                                    fi
                                else
                                    echo "ERROR: Failed to move temp file to final location"
                                    [[ -f "$TEMP_FILE" ]] && rm "$TEMP_FILE"
                                fi
                            else
                                echo "ERROR: Temp file was not created successfully"
                                [[ -f "$TEMP_FILE" ]] && rm "$TEMP_FILE"
                            fi
                        else
                            echo "Failed to embed album art in ${FILENAME}"
                            # Clean up temp file if it exists
                            [[ -f "$TEMP_FILE" ]] && rm "$TEMP_FILE"
                        fi
                    else
                        echo "Failed to download image: ${UUID}.jpeg"
                    fi
                else
                    echo "Image ${UUID}.jpeg already exists, checking if MP3 needs embedding..."
                    # If image exists but MP3 doesn't have embedded art, embed it
                    FINAL_FILE="${DEST_DIR}/${FILENAME}"
                    TEMP_FILE="${DEST_DIR}/temp_${FILENAME}"
                    if [[ ! -f "$FINAL_FILE" ]]; then
                        echo "Embedding existing album art in ${FILENAME}..."
                        if ffmpeg -i "$DEST_FILE" -i "$IMAGE_FILE" -c copy -map 0 -map 1 -c:v copy -metadata:s:v title="Album cover" -metadata:s:v comment="Cover (front)" -disposition:v attached_pic -y "$TEMP_FILE" 2>/dev/null; then
                            echo "Successfully created temp file with embedded art"
                            if [[ -f "$TEMP_FILE" ]]; then
                                mv "$TEMP_FILE" "$FINAL_FILE"
                                echo "Successfully created final file: $(basename "$FINAL_FILE")"
                                # DON'T remove DEST_FILE since it's the same as FINAL_FILE now!
                                echo "File ready with embedded album art"
                            else
                                echo "ERROR: Temp file was not created successfully"
                                [[ -f "$TEMP_FILE" ]] && rm "$TEMP_FILE"
                            fi
                        else
                            echo "Failed to embed existing album art in ${FILENAME}"
                            [[ -f "$TEMP_FILE" ]] && rm "$TEMP_FILE"
                        fi
                    else
                        echo "Final MP3 file already exists, skipping embedding"
                    fi
                fi
            else
                # For MP4 files, just confirm download is complete
                echo "MP4 file ready: $(basename "$DEST_FILE")"
            fi
        else
            echo "Failed to download file: ${FILENAME}"
        fi
    fi
done < "$SONGFILE"

echo "All files downloaded to: $DEST_DIR"