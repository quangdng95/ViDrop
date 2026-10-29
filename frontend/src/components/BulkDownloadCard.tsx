import { useCallback, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import {
  Download,
  Loader2,
  XCircle,
  RefreshCw,
  FolderOpen,
  FolderCheck,
  FolderX,
  Check,
  AlertCircle,
  Save,
  Image as ImageIcon,
  Film,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Progress } from "@/components/ui/progress";
import PlatformBlock from "./PlatformBlock";
import PlatformTag from "./PlatformTag";
import { ctaGhost, ctaLight, progressSlab, rowButton } from "../lib/blockStyles";
import { useLanguage } from "../i18n/LanguageContext";
import { preparePlainFile, type PrepareOptions } from "../lib/saveFile";
import { usePreparedSave } from "../hooks/usePreparedSave";
import { api } from "../api";
import { isLocal } from "../isLocal";
import { requestNotifyPermission, notifyIfHidden } from "../lib/notify";
import { recordDownload } from "../lib/history";
import type { JobProgress, MediaKind, Platform } from "../types";

// How many links download at once. Mirrors BATCH_CONCURRENCY in backend/app.py
// (the server-side limit for a playlist's own batch job) - a sensible default
// for a small shared machine, and plenty for the local desktop app too.
const CONCURRENCY = 3;
const POLL_INTERVAL_MS = 700;

type RowStatus = "queued" | "checking" | "downloading" | "done" | "error" | "skipped";

interface BulkRow {
  id: string;
  url: string;
  status: RowStatus;
  title: string | null;
  platform: Platform | null;
  thumbnail: string | null;
  kind?: MediaKind;
  percent: number;
  jobId: string | null;
  // Set once a job is done on a remote deployment, so the row can offer its own Save.
  downloadUrl?: string;
  errorMessage: string | null;
  // False for a line that was never a real downloadable link (not a URL, or a
  // playlist/channel) - retrying it would just fail the same way again.
  retryable: boolean;
}

const URL_LINE = /^https?:\/\/\S+$/i;

// Parses the textarea into one candidate per non-empty line, de-duplicated.
const parseLines = (raw: string): string[] => {
  const seen = new Set<string>();
  const out: string[] = [];
  raw
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .forEach((line) => {
      if (seen.has(line)) return;
      seen.add(line);
      out.push(line);
    });
  return out;
};

const emptyProgress = (status: JobProgress["status"]): JobProgress => ({
  status,
  percent: 0,
  text: "",
  filename: null,
});

// Polls one job's /api/progress until it leaves "running", calling onUpdate on
// every tick. Bails out early (without touching the server-side job) once
// isCancelled() turns true, so a Cancel click or an unmount stops polling
// immediately instead of waiting out the next 700ms tick.
const pollJob = (jobId: string, isCancelled: () => boolean, onUpdate: (p: JobProgress) => void): Promise<JobProgress> =>
  new Promise((resolve) => {
    const id = setInterval(() => {
      if (isCancelled()) {
        clearInterval(id);
        resolve(emptyProgress("cancelled"));
        return;
      }
      api
        .getProgress(jobId)
        .then((progress) => {
          if (isCancelled()) {
            clearInterval(id);
            resolve(emptyProgress("cancelled"));
            return;
          }
          onUpdate(progress);
          if (progress.status !== "running") {
            clearInterval(id);
            resolve(progress);
          }
        })
        .catch(() => {
          clearInterval(id);
          resolve(emptyProgress("error"));
        });
    }, POLL_INTERVAL_MS);
  });

const BulkDownloadCard = () => {
  const { t } = useLanguage();
  const [text, setText] = useState("");
  const [rows, setRows] = useState<BulkRow[]>([]);
  const [running, setRunning] = useState(false);
  const cancelledRef = useRef(false);
  const activeJobIdsRef = useRef<Set<string>>(new Set());

  const parsedCount = useMemo(() => parseLines(text).filter((line) => URL_LINE.test(line)).length, [text]);

  const updateRow = (id: string, patch: Partial<BulkRow>) =>
    setRows((prev) => prev.map((r) => (r.id === id ? { ...r, ...patch } : r)));

  const announceResult = (saved: number, failed: number) => {
    if (saved <= 0) return;
    const head = (isLocal() ? t.downloadSuccess.batchSaved : t.downloadSuccess.batchReady).replace(
      "{saved}",
      String(saved)
    );
    const message = failed > 0 ? head + t.downloadSuccess.failedSuffix.replace("{failed}", String(failed)) : head;
    if (isLocal()) toast.success(message, { description: t.bulk.heading });
    notifyIfHidden(message, t.bulk.heading);
  };

  // Resolves one row's link, starts its download and waits for it to finish.
  // Returns how it ended, so the caller can tally a running save/fail count
  // without re-reading React state (which lags the async work by a render).
  const processRow = async (row: BulkRow): Promise<"done" | "error" | "skipped" | "cancelled"> => {
    if (cancelledRef.current) return "cancelled";
    updateRow(row.id, { status: "checking", errorMessage: null });
    let check;
    try {
      check = await api.checkLink(row.url);
    } catch (e) {
      if (cancelledRef.current) return "cancelled";
      updateRow(row.id, { status: "error", errorMessage: (e as Error).message });
      return "error";
    }
    if (cancelledRef.current) return "cancelled";

    let title: string;
    let platform: Platform;
    let thumbnail: string | null;
    let quality: string;
    let kind: MediaKind | undefined;
    let dlUrl: string;
    let entryIndex: number | undefined;

    if (check.type === "video") {
      title = check.title;
      platform = check.platform;
      thumbnail = check.thumbnail;
      quality = check.qualities[0] ?? "Best";
      kind = check.kind;
      dlUrl = row.url;
    } else if (check.type === "playlist" && check.items.length === 1) {
      const item = check.items[0];
      title = item.title;
      platform = check.platform;
      thumbnail = item.thumbnail;
      quality = item.qualities[0] ?? "Best";
      kind = item.kind;
      dlUrl = item.url ?? row.url;
      entryIndex = item.url ? undefined : item.entry_index;
    } else {
      updateRow(row.id, {
        status: "skipped",
        title: check.type === "playlist" ? check.title || row.url : row.url,
        platform: check.platform,
        errorMessage: t.bulk.playlistUnsupported,
        retryable: false,
      });
      return "skipped";
    }

    updateRow(row.id, { status: "downloading", title, platform, thumbnail, kind });

    let jobId: string;
    try {
      const started = await api.startDownload(dlUrl, title, quality, entryIndex);
      jobId = started.job_id;
    } catch (e) {
      if (cancelledRef.current) return "cancelled";
      updateRow(row.id, { status: "error", errorMessage: (e as Error).message });
      return "error";
    }
    updateRow(row.id, { jobId });
    activeJobIdsRef.current.add(jobId);
    const final = await pollJob(
      jobId,
      () => cancelledRef.current,
      (p) => updateRow(row.id, { percent: p.percent })
    );
    activeJobIdsRef.current.delete(jobId);

    if (final.status === "done") {
      updateRow(row.id, {
        status: "done",
        percent: 100,
        downloadUrl: isLocal() ? undefined : `/api/download-file/${jobId}`,
      });
      recordDownload({ url: row.url, title, platform, thumbnail });
      return "done";
    }
    if (final.status === "cancelled") {
      updateRow(row.id, { status: "error", errorMessage: t.bulk.cancelled });
      return "cancelled";
    }
    updateRow(row.id, { status: "error", errorMessage: final.text || t.bulk.genericFailed });
    return "error";
  };

  const runAll = async (queue: BulkRow[]) => {
    if (queue.length === 0) return;
    cancelledRef.current = false;
    setRunning(true);
    let cursor = 0;
    let saved = 0;
    let failed = 0;
    const worker = async () => {
      for (;;) {
        if (cancelledRef.current) return;
        const index = cursor++;
        if (index >= queue.length) return;
        const outcome = await processRow(queue[index]);
        if (outcome === "done") saved++;
        else if (outcome === "error") failed++;
      }
    };
    await Promise.all(Array.from({ length: Math.min(CONCURRENCY, queue.length) }, worker));
    setRunning(false);
    announceResult(saved, failed);
  };

  const handleStart = () => {
    const lines = text
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter(Boolean);
    const seen = new Set<string>();
    const newRows: BulkRow[] = [];
    lines.forEach((line, i) => {
      const valid = URL_LINE.test(line);
      if (valid) {
        if (seen.has(line)) return;
        seen.add(line);
      }
      newRows.push({
        id: `${Date.now()}-${i}`,
        url: line,
        status: valid ? "queued" : "error",
        title: valid ? null : line,
        platform: null,
        thumbnail: null,
        percent: 0,
        jobId: null,
        errorMessage: valid ? null : t.bulk.invalidUrl,
        retryable: valid,
      });
    });
    if (newRows.length === 0) return;
    requestNotifyPermission();
    setRows(newRows);
    setText("");
    void runAll(newRows.filter((r) => r.status === "queued"));
  };

  const handleCancel = () => {
    cancelledRef.current = true;
    activeJobIdsRef.current.forEach((id) => void api.cancelJob(id));
    activeJobIdsRef.current.clear();
    setRows((prev) =>
      prev.map((r) =>
        r.status === "queued" || r.status === "checking" || r.status === "downloading"
          ? { ...r, status: "error", errorMessage: t.bulk.cancelled }
          : r
      )
    );
    setRunning(false);
  };

  const handleRetryRow = (id: string) => {
    const row = rows.find((r) => r.id === id);
    if (!row || !row.retryable) return;
    updateRow(id, { status: "queued", errorMessage: null, percent: 0 });
    void runAll([{ ...row, status: "queued", errorMessage: null, percent: 0 }]);
  };

  const handleRetryFailed = () => {
    const failed = rows.filter((r) => r.status === "error" && r.retryable);
    if (failed.length === 0) return;
    setRows((prev) =>
      prev.map((r) => (failed.some((f) => f.id === r.id) ? { ...r, status: "queued", errorMessage: null, percent: 0 } : r))
    );
    void runAll(failed.map((r) => ({ ...r, status: "queued", errorMessage: null, percent: 0 })));
  };

  const handleReset = () => {
    setRows([]);
    setText("");
  };

  const handleOpenFolder = () => void api.openFolder();

  const doneCount = rows.filter((r) => r.status === "done").length;
  const failedCount = rows.filter((r) => r.status === "error" && r.retryable).length;
  const totalFailedOrSkipped = rows.filter((r) => r.status === "error" || r.status === "skipped").length;
  const overallPercent = rows.length
    ? rows.reduce((sum, r) => sum + (r.status === "done" ? 100 : r.status === "downloading" ? r.percent : 0), 0) /
      rows.length
    : 0;
  const numWidth = Math.max(2, String(rows.length).length);

  return (
    <div className="flex w-full flex-col gap-6">
      <div className="flex flex-col gap-4">
        <div className="flex flex-col gap-2">
          <Label htmlFor="bulk-input" className="font-display text-lg leading-tight font-bold text-ink">
            {t.bulk.fieldLabel}
          </Label>
          <Textarea
            id="bulk-input"
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder={t.bulk.placeholder}
            disabled={running}
            rows={6}
            className="focus-inset min-h-[9.5rem] w-full resize-y rounded-none border-[3px] border-ink bg-white px-[18px] py-4 text-lg leading-snug font-medium text-ink shadow-hard disabled:opacity-100 disabled:bg-muted md:px-7 md:text-xl"
          />
        </div>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <span className="text-base font-medium text-ink">
            {t.bulk.linksCount.replace("{n}", String(parsedCount))}
          </span>
          <button type="button" onClick={handleStart} disabled={running || parsedCount === 0} className={ctaLight}>
            {running ? <Loader2 className="h-6 w-6 animate-spin" aria-hidden /> : <Download className="h-6 w-6" aria-hidden />}
            {t.playlist.downloadAll}
          </button>
        </div>
      </div>

      {rows.length > 0 && (
        <PlatformBlock tone="ink" role="status">
          <div className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
            <p className="m-0 min-w-0 text-lg">
              {t.playlist.totalItems} <strong className="font-bold tabular-nums">{rows.length}</strong>
            </p>
            {running ? (
              <button type="button" onClick={handleCancel} className={ctaGhost}>
                <XCircle className="h-5 w-5" aria-hidden />
                {t.downloadProgress.cancelDownload}
              </button>
            ) : (
              <button type="button" onClick={handleReset} className={ctaGhost}>
                <RefreshCw className="h-5 w-5" aria-hidden />
                {t.bulk.newBatch}
              </button>
            )}
          </div>

          {running && (
            <div className="flex w-full flex-col gap-4">
              <p className="m-0 font-display text-xl font-bold">
                {t.playlist.downloadedProgress.replace("{done}", String(doneCount)).replace("{total}", String(rows.length))}
              </p>
              <Progress value={overallPercent} aria-label={t.playlist.downloading} className={progressSlab} />
            </div>
          )}

          <ul className="m-0 flex w-full list-none flex-col p-0">
            {rows.map((row, index) => (
              <BulkRowItem
                key={row.id}
                row={row}
                position={index + 1}
                numWidth={numWidth}
                onRetry={() => handleRetryRow(row.id)}
              />
            ))}
          </ul>

          {!running && (doneCount > 0 || totalFailedOrSkipped > 0) && (
            <div className="flex w-full flex-col gap-4">
              <div className="flex flex-wrap items-center justify-between gap-3 font-display text-lg font-bold">
                <span className="inline-flex items-center gap-2">
                  <FolderX className="h-5 w-5 shrink-0" aria-hidden />
                  {t.playlist.failedItems.replace("{n}", String(totalFailedOrSkipped))}
                </span>
                <span className="inline-flex items-center gap-2">
                  <FolderCheck className="h-5 w-5 shrink-0" aria-hidden />
                  {t.playlist.savedItems.replace("{n}", String(doneCount))}
                </span>
              </div>
              <div className="flex flex-col gap-4 sm:flex-row sm:flex-wrap">
                {failedCount > 0 && (
                  <button type="button" onClick={handleRetryFailed} className={ctaGhost}>
                    <RefreshCw className="h-5 w-5" aria-hidden />
                    {t.playlist.retry}
                  </button>
                )}
                {isLocal() && doneCount > 0 && (
                  <button type="button" onClick={handleOpenFolder} className={ctaLight}>
                    <FolderOpen className="h-6 w-6" aria-hidden />
                    {t.downloadSuccess.openFolder}
                  </button>
                )}
              </div>
            </div>
          )}
        </PlatformBlock>
      )}
    </div>
  );
};

interface BulkRowItemProps {
  row: BulkRow;
  position: number;
  numWidth: number;
  onRetry: () => void;
}

const BulkRowItem = ({ row, position, numWidth, onRetry }: BulkRowItemProps) => {
  const { t } = useLanguage();
  const prepare = useCallback(
    (url: string, options: PrepareOptions) => preparePlainFile(url, row.title || row.url, options),
    [row.title, row.url]
  );
  const { saving, failed: saveFailed, preparing, progress, shareKind, save } = usePreparedSave(
    row.status === "done" ? row.downloadUrl : undefined,
    prepare
  );
  const saveBusy = saving || preparing;
  const saveLabel = preparing
    ? progress === null
      ? t.downloadSuccess.preparingNoPercent
      : t.downloadSuccess.preparing.replace("{percent}", String(Math.round(progress)))
    : saveFailed
      ? t.downloadSuccess.tryAgain
      : shareKind === "photos"
        ? t.downloadSuccess.saveToPhotos
        : t.downloadSuccess.save;

  const formattedNum = String(position).padStart(numWidth, "0");

  return (
    <li className="flex min-h-[76px] list-none flex-wrap items-center gap-x-3 gap-y-2 border-t-[3px] border-current py-3 last:border-b-[3px] sm:flex-nowrap sm:gap-4">
      <span className="w-9 shrink-0 text-right font-display text-[1.375rem] font-bold tabular-nums">{formattedNum}.</span>

      <div className="flex h-12 w-16 shrink-0 items-center justify-center overflow-hidden border-2 border-current bg-black/30">
        {row.thumbnail ? (
          <img src={row.thumbnail} referrerPolicy="no-referrer" alt="" className="h-full w-full object-cover" />
        ) : row.status === "checking" || row.status === "queued" ? (
          <Loader2 className="h-5 w-5 animate-spin" aria-hidden />
        ) : row.kind === "image" ? (
          <ImageIcon className="h-5 w-5" aria-hidden />
        ) : (
          <Film className="h-5 w-5" aria-hidden />
        )}
      </div>

      <div className="flex min-w-0 flex-1 flex-col">
        <h4 className="m-0 line-clamp-2 font-display text-[clamp(1.125rem,3.2vw,1.625rem)] leading-[1.15] font-bold tracking-[-0.02em] break-all">
          {row.title || row.url}
        </h4>
        <p className="m-0 flex flex-wrap items-center gap-x-3 text-base">
          {row.platform && <PlatformTag platform={row.platform} />}
          {row.status === "checking" && <span className="font-bold uppercase">{t.urlInput.checking}</span>}
          {row.status === "queued" && <span className="font-bold uppercase">{t.playlist.queued}</span>}
          {(row.status === "error" || row.status === "skipped") && row.errorMessage && (
            <span className="font-bold">{row.errorMessage}</span>
          )}
        </p>
      </div>

      <div className="flex w-full shrink-0 flex-row items-center justify-between gap-2 sm:w-[11.5rem] sm:flex-col sm:items-end sm:justify-center">
        {row.status === "downloading" && (
          <div role="status" className="flex w-full flex-col gap-1.5">
            <span className="font-display text-base font-bold">
              {t.playlist.percentDownloading.replace("{p}", String(Math.round(row.percent)))}
            </span>
            <Progress
              value={row.percent}
              aria-label={t.playlist.downloading}
              className={cn(progressSlab, "[&_[data-slot=progress-track]]:h-3.5")}
            />
          </div>
        )}

        {row.status === "done" && (
          <div className="flex w-full flex-row items-center justify-between gap-2 sm:w-auto sm:flex-col sm:items-end">
            <span className="inline-flex items-center gap-1.5 font-display text-base font-bold uppercase">
              <Check className="h-5 w-5" strokeWidth={3} aria-hidden />
              {t.playlist.downloaded}
            </span>
            {row.downloadUrl && (
              <button type="button" className={rowButton} onClick={save} disabled={saveBusy}>
                {saveBusy ? <Loader2 className="h-5 w-5 animate-spin" aria-hidden /> : <Save className="h-5 w-5" aria-hidden />}
                {saveLabel}
              </button>
            )}
          </div>
        )}

        {(row.status === "error" || row.status === "skipped") && (
          <div className="flex w-full flex-row items-center justify-between gap-2 sm:w-auto sm:flex-col sm:items-end">
            <span className="inline-flex items-center gap-1.5 font-display text-base font-bold uppercase">
              <AlertCircle className="h-5 w-5" aria-hidden />
              {t.playlist.failed}
            </span>
            {row.status === "error" && row.retryable && (
              <button type="button" className={rowButton} onClick={onRetry}>
                <RefreshCw className="h-5 w-5" aria-hidden />
                {t.playlist.retry}
              </button>
            )}
          </div>
        )}
      </div>
    </li>
  );
};

export default BulkDownloadCard;
