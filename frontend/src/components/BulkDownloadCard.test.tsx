import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import BulkDownloadCard from "./BulkDownloadCard";
import { api } from "../api";
import * as isLocalModule from "../isLocal";
import { LanguageProvider } from "../i18n/LanguageContext";
import type { VideoInfo } from "../types";

vi.mock("../api", () => ({
  api: {
    checkLink: vi.fn(),
    startDownload: vi.fn(),
    getProgress: vi.fn(),
    cancelJob: vi.fn(),
    openFolder: vi.fn(),
  },
}));
vi.mock("../lib/saveFile", () => ({
  preparePlainFile: vi.fn(() => Promise.resolve({ files: [], fallbackBlob: new Blob(), fallbackFilename: "x" })),
  sharePreparedSave: vi.fn().mockResolvedValue("shared"),
  isNotAllowedError: () => false,
  isAbortError: (e: unknown) => e instanceof Error && e.name === "AbortError",
  getSavePlatform: vi.fn(() => "desktop"),
}));

const mockedApi = vi.mocked(api, true);

const VIDEO_A: VideoInfo = {
  type: "video",
  title: "Clip A",
  uploader: "U",
  thumbnail: null,
  platform: "YouTube",
  qualities: ["Best", "720p"],
  duration: "00:30",
};

const renderCard = () =>
  render(
    <LanguageProvider>
      <BulkDownloadCard />
    </LanguageProvider>
  );

describe("BulkDownloadCard", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.spyOn(isLocalModule, "isLocal").mockReturnValue(true);
  });

  it("downloads every valid link pasted at once, and flags an invalid line without calling the API for it", async () => {
    mockedApi.checkLink.mockResolvedValue(VIDEO_A);
    mockedApi.startDownload.mockResolvedValue({ job_id: "job-1" });
    mockedApi.getProgress.mockResolvedValue({ status: "done", percent: 100, text: "", filename: "Clip A.mp4" });

    const user = userEvent.setup({ delay: null });
    renderCard();

    const textarea = screen.getByPlaceholderText("Paste multiple links, one per line");
    await user.click(textarea);
    await user.paste("https://youtube.com/watch?v=a\nnot a link at all");

    expect(screen.getByText("1 links ready to download")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /download all/i }));

    await waitFor(() => expect(mockedApi.checkLink).toHaveBeenCalledTimes(1));
    expect(mockedApi.checkLink).toHaveBeenCalledWith("https://youtube.com/watch?v=a");

    await screen.findByText("Not a valid link");

    await waitFor(() => expect(mockedApi.startDownload).toHaveBeenCalledTimes(1));
    expect(mockedApi.startDownload).toHaveBeenCalledWith("https://youtube.com/watch?v=a", "Clip A", "Best", undefined);

    await screen.findByText("Clip A");
    await waitFor(() => expect(screen.getAllByText("Downloaded")).toHaveLength(1));

    // The textarea clears once the batch is kicked off, ready for a new paste.
    expect(textarea).toHaveValue("");
  });

  it("skips a playlist/channel link instead of expanding it", async () => {
    mockedApi.checkLink.mockResolvedValue({
      type: "playlist",
      platform: "YouTube",
      title: "A whole channel",
      items: [
        { id: "1", title: "Item 1", thumbnail: null, duration: null, qualities: ["Best"] },
        { id: "2", title: "Item 2", thumbnail: null, duration: null, qualities: ["Best"] },
      ],
    });

    const user = userEvent.setup({ delay: null });
    renderCard();

    await user.click(screen.getByPlaceholderText("Paste multiple links, one per line"));
    await user.paste("https://youtube.com/playlist?list=PLx");
    await user.click(screen.getByRole("button", { name: /download all/i }));

    await screen.findByText(/paste it on its own to pick items/i);
    expect(mockedApi.startDownload).not.toHaveBeenCalled();
  });

  it("cancels every in-flight job and marks pending rows as cancelled", async () => {
    mockedApi.checkLink.mockResolvedValue(VIDEO_A);
    mockedApi.startDownload.mockResolvedValue({ job_id: "job-2" });
    // Never resolves to "done" on its own - the row stays "downloading" until Cancel is pressed.
    mockedApi.getProgress.mockResolvedValue({ status: "running", percent: 10, text: "", filename: null });

    const user = userEvent.setup({ delay: null });
    renderCard();

    await user.click(screen.getByPlaceholderText("Paste multiple links, one per line"));
    await user.paste("https://youtube.com/watch?v=a");
    await user.click(screen.getByRole("button", { name: /download all/i }));

    await waitFor(() => expect(mockedApi.startDownload).toHaveBeenCalledTimes(1));
    await user.click(screen.getByRole("button", { name: /cancel download/i }));

    expect(mockedApi.cancelJob).toHaveBeenCalledWith("job-2");
    await screen.findByText(/cancelled/i);
  });
});
