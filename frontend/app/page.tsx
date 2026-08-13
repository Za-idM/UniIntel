"use client";

import { useEffect, useRef, useState } from "react";
import Header, { type Tab } from "@/components/Header";
import UploadView from "@/components/UploadView";
import ProductView from "@/components/ProductView";
import EvaluateView from "@/components/EvaluateView";
import { getJob, getJobResults, type JobStatusResponse, type ProcessResponse, type ProductRow } from "@/lib/api";

const POLL_INTERVAL_MS = 1500;
const JOB_QUERY_PARAM = "job";

function setJobQueryParam(jobId: string | null) {
  const url = new URL(window.location.href);
  if (jobId) {
    url.searchParams.set(JOB_QUERY_PARAM, jobId);
  } else {
    url.searchParams.delete(JOB_QUERY_PARAM);
  }
  window.history.replaceState(null, "", url.toString());
}

export default function Home() {
  const [tab, setTab] = useState<Tab>("upload");
  const [jobId, setJobId] = useState<string | null>(null);
  const [jobStatus, setJobStatus] = useState<JobStatusResponse | null>(null);
  const [jobProducts, setJobProducts] = useState<ProductRow[]>([]);
  const [selectedProductId, setSelectedProductId] = useState<string | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Rehydrate from ?job=<id> on first load (e.g. after a browser refresh)
  // so an in-progress or finished job isn't lost. A missing/invalid/stale
  // job_id just falls back to the normal empty state -- never surfaces a
  // raw error -- and gets scrubbed from the URL so a retried refresh
  // doesn't keep failing the same way.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const urlJobId = params.get(JOB_QUERY_PARAM);
    if (!urlJobId) return;

    getJob(urlJobId)
      .then((status) => {
        setJobStatus(status);
        setJobId(urlJobId);
      })
      .catch(() => {
        setJobQueryParam(null);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Keep the URL in sync with the active job so a refresh has something to
  // rehydrate from.
  useEffect(() => {
    setJobQueryParam(jobId);
  }, [jobId]);

  // Polling lives here, not inside UploadView, so it keeps running (and
  // jobStatus/jobProducts stay live) no matter which tab is active --
  // switching to Product Detail or Evaluation mid-job no longer stops
  // progress from updating in the background.
  useEffect(() => {
    if (!jobId) return;

    const poll = async () => {
      try {
        const [status, results] = await Promise.all([getJob(jobId), getJobResults(jobId)]);
        setJobStatus(status);
        setJobProducts(results.products);
        if (status.status === "DONE" || status.status === "FAILED") {
          if (pollRef.current) clearInterval(pollRef.current);
        }
      } catch {
        // transient fetch failure -- next tick retries, don't kill the poll
      }
    };

    poll();
    pollRef.current = setInterval(poll, POLL_INTERVAL_MS);
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [jobId]);

  const handleStartJob = (res: ProcessResponse) => {
    setJobId(res.job_id);
    setJobStatus({
      id: res.job_id,
      status: res.status,
      input_filename: res.filename,
      total_rows: res.total_rows,
      processed_rows: 0,
      error: null,
      created_at: "",
      updated_at: "",
    });
    setJobProducts([]);
  };

  const handleSelectProduct = (id: string) => {
    setSelectedProductId(id);
    setTab("product");
  };

  return (
    <div className="min-h-screen bg-paper">
      <Header active={tab} onChange={setTab} jobId={jobId} />
      {tab === "upload" && (
        <UploadView
          onStartJob={handleStartJob}
          onSelectProduct={handleSelectProduct}
          jobStatus={jobStatus}
          jobProducts={jobProducts}
        />
      )}
      {tab === "product" && (
        <ProductView
          productId={selectedProductId}
          jobProducts={jobProducts}
          onSelectProduct={setSelectedProductId}
        />
      )}
      {tab === "evaluate" && (
        <EvaluateView jobId={jobId} jobStatus={jobStatus} onSelectProduct={handleSelectProduct} />
      )}
    </div>
  );
}
