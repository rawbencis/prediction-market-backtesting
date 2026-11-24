#!/usr/bin/env bash
# Archive recorder day folders older than N days: zstd each hourly .bin file, verify, then delete the raw copy.
# Dublin 11-25 (D2). Runs as user ubuntu, no sudo. DRY RUN unless --apply.
#
# This box has no AWS CLI and no instance role (checked 2026-11-24: `aws` missing, IMDS iam/info = 404), so the
# default target is a LOCAL archive (default ~/archive). On the same disk this frees only the compression gain:
# measured on 2026-11-20 hour 12 at zstd -10: pm 4.0x, bn 3.8x, act 3.4x, tr 1.45x  ->  ~74% of a shadow-rec day.
# feeds-rec files are already gzip (zstd -10 on top gains 1.2x, -19 takes 7 min per pmbook hour), so they are
# skipped unless --include-gz.
# --s3 s3://bucket/prefix: UNTESTED (no credentials here). After the local .zst is verified, upload it with
# its sha256 as metadata, then check the object's size and sha256 with head-object; only then delete the local .zst too.
#
# Usage:
#   deploy/archive-recordings.sh [--apply] [--older-than 14] [--protect 2026-11-15..2026-12-08]... \
#        [--src ~/shadow-rec] [--src ~/hourly-rec] [--dest ~/archive] [--include-gz] [--level 10] [--s3 s3://b/p]
# Defaults: --older-than 14, --src ~/shadow-rec and ~/hourly-rec, --dest ~/archive, level 10.
# Restore one day:  mkdir -p ~/shadow-rec/D && for f in ~/archive/shadow-rec/D/*.zst; do zstd -dq "$f" -o ~/shadow-rec/D/$(basename "$f" .zst); done
#                   then compare `sha256sum ~/shadow-rec/D/*.bin` with ~/archive/shadow-rec/D/SHA256SUMS.raw ("sha size  name").
#                   Evaluators read only raw .bin files: an archived day is invisible to them until restored.
# Per archived day: DEST/<src>/<day>/<file>.zst and SHA256SUMS.raw (sha256 and byte size of each raw file).
set -euo pipefail

APPLY=0; OLDER=14; DEST="$HOME/archive"; LEVEL=10; GZ=0; S3=""
SRCS=(); PROTECT=()
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --older-than) OLDER="$2"; shift ;;
    --protect) PROTECT+=("$2"); shift ;;
    --src) SRCS+=("$2"); shift ;;
    --dest) DEST="$2"; shift ;;
    --level) LEVEL="$2"; shift ;;
    --include-gz) GZ=1 ;;
    --s3) S3="$2"; shift ;;
    -h|--help) sed -n 2,25p "$0"; exit 0 ;;
    *) echo "unknown arg $1" >&2; exit 2 ;;
  esac
  shift
done
[ ${#SRCS[@]} -eq 0 ] && SRCS=("$HOME/shadow-rec" "$HOME/hourly-rec")
[ "$OLDER" -ge 2 ] || { echo "--older-than must be >= 2 (never touch a day a recorder may still write)" >&2; exit 2; }
if [ -n "$S3" ]; then command -v aws >/dev/null || { echo "--s3 given but no aws CLI" >&2; exit 2; }; fi

exec 9>"$HOME/.archive-recordings.lock"
flock -n 9 || { echo "another archive run holds the lock" >&2; exit 1; }

CUT=$(date -u -d "-$OLDER days" +%F)
protected() {  # $1 = YYYY-MM-DD
  local r a b
  for r in "${PROTECT[@]+"${PROTECT[@]}"}"; do a=${r%%..*}; b=${r##*..}; [[ "$1" > "$a" || "$1" == "$a" ]] && [[ "$1" < "$b" || "$1" == "$b" ]] && return 0; done
  return 1
}
log() { echo "$(date -u +%FT%TZ) $*"; }

tot_raw=0; tot_zst=0; nfiles=0; nfail=0
log "mode=$([ $APPLY = 1 ] && echo APPLY || echo DRY-RUN) cut=<$CUT dest=$DEST level=$LEVEL gz=$GZ s3=${S3:-none} protect=${PROTECT[*]-none}"
for src in "${SRCS[@]}"; do
  [ -d "$src" ] || continue
  name=$(basename "$src")
  for dd in "$src"/????-??-??; do
    [ -d "$dd" ] || continue
    day=$(basename "$dd")
    [[ "$day" < "$CUT" ]] || continue
    if protected "$day"; then log "skip protected $name/$day"; continue; fi
    pat=(-name '*.bin'); [ $GZ = 1 ] && pat=(-name '*.bin' -o -name '*.jsonl.gz')
    mapfile -t files < <(find "$dd" -maxdepth 1 -type f \( "${pat[@]}" \) | sort)
    [ ${#files[@]} -eq 0 ] && continue
    out="$DEST/$name/$day"
    if [ $APPLY = 0 ]; then
      sz=$(du -cb "${files[@]}" | tail -1 | cut -f1); tot_raw=$((tot_raw + sz)); nfiles=$((nfiles + ${#files[@]}))
      log "would archive $name/$day: ${#files[@]} files, $((sz / 1000000)) MB"
      continue
    fi
    mkdir -p "$out"
    for f in "${files[@]}"; do
      b=$(basename "$f"); z="$out/$b.zst"
      # a .jsonl.gz is stored as zstd of its decompressed content; its hash is of that content
      if [[ "$b" == *.gz ]]; then raw_sha=$(zcat "$f" | sha256sum | cut -d' ' -f1); raw_n=$(zcat "$f" | wc -c)
        zcat "$f" | nice -n 19 ionice -c3 zstd -q -T1 -"$LEVEL" --long=27 -o "$z.tmp" -f
      else raw_sha=$(sha256sum "$f" | cut -d' ' -f1); raw_n=$(stat -c %s "$f")
        nice -n 19 ionice -c3 zstd -q -T1 -"$LEVEL" "$f" -o "$z.tmp" -f
      fi
      chk_sha=$(zstd -dc --long=27 "$z.tmp" | sha256sum | cut -d' ' -f1)
      chk_n=$(zstd -dc --long=27 "$z.tmp" | wc -c)
      if [ "$chk_sha" != "$raw_sha" ] || [ "$chk_n" != "$raw_n" ]; then
        log "VERIFY FAILED $f (sha/size mismatch): raw kept"; rm -f "$z.tmp"; nfail=$((nfail + 1)); continue
      fi
      mv -f "$z.tmp" "$z"
      zn=$(stat -c %s "$z")
      grep -v "  $b\$" "$out/SHA256SUMS.raw" 2>/dev/null > "$out/.sums.tmp" || true
      printf '%s %s  %s\n' "$raw_sha" "$raw_n" "$b" >> "$out/.sums.tmp"; mv -f "$out/.sums.tmp" "$out/SHA256SUMS.raw"
      if [ -n "$S3" ]; then  # UNTESTED
        zsha=$(sha256sum "$z" | cut -d' ' -f1); key="${S3%/}/$name/$day/$b.zst"
        aws s3 cp --only-show-errors "$z" "$key" --metadata "sha256=$zsha,raw-sha256=$raw_sha"
        bucket=${key#s3://}; bucket=${bucket%%/*}; k=${key#s3://$bucket/}
        read -r rlen rsha < <(aws s3api head-object --bucket "$bucket" --key "$k" --query '[ContentLength, Metadata.sha256]' --output text)
        if [ "$rlen" != "$zn" ] || [ "$rsha" != "$zsha" ]; then log "S3 VERIFY FAILED $key: local kept"; nfail=$((nfail + 1)); continue; fi
        rm -f "$z"
      fi
      rm -f "$f"
      tot_raw=$((tot_raw + raw_n)); tot_zst=$((tot_zst + zn)); nfiles=$((nfiles + 1))
    done
    log "archived $name/$day -> $out"
  done
done
log "files=$nfiles raw_MB=$((tot_raw / 1000000)) zst_MB=$((tot_zst / 1000000)) failures=$nfail"
[ $nfail -eq 0 ]
