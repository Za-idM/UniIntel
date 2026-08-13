// Thin fetch layer + types matching backend/schemas/product.py and the
// FastAPI route responses (backend/api/*.py). No auth (Locked Decision #4).

export const API_BASE = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000";

export type AttributeOrigin = "rule_prior" | "llm_extract" | null;

export interface AttributeValue {
  slot: number;
  label: string;
  value: string | null;
  uom: string | null;
  source_url: string | null;
  evidence_text: string | null;
  lov_valid: boolean | null;
  confidence: number | null;
  origin: AttributeOrigin;
}

export interface Descriptions {
  invoice_desc: string | null;
  mobile_desc: string | null;
  short_desc: string | null;
  long_desc1: string | null;
  retail_desc: string | null;
  marketing_description: string | null;
}

export interface ValidationResult {
  v1_required: boolean;
  v2_lov: boolean;
  v3_uom_inline: boolean;
  v4_casing_inline: boolean;
  v5_brand_mfr: boolean;
  v6_source_url: boolean;
  warnings: string[];
  needs_human_review: boolean;
}

export type ConfidenceBand = "VERIFIED" | "REVIEW" | "LOW";

export interface EnrichedProduct {
  product_id: string;
  job_id: string;
  mfg_part_num: string;
  part_desc: string;
  part_manuf_raw: string | null;
  manufacturer_name: string | null;
  brand_name: string | null;
  classpath: string | null;
  mfr_url: string | null;
  ref_urls: string[];
  attributes: AttributeValue[];
  descriptions: Descriptions;
  validation: ValidationResult;
  confidence: number;
  confidence_band: ConfidenceBand;
}

export interface ProductRow {
  id: string;
  job_id: string;
  mfg_part_num: string;
  part_desc: string;
  part_manuf_raw: string | null;
  manufacturer_name: string | null;
  brand_name: string | null;
  classpath: string | null;
  mfr_url: string | null;
  confidence: number;
  confidence_band: ConfidenceBand;
  created_at: string;
  updated_at: string;
}

export interface ProductDetail extends ProductRow {
  data: EnrichedProduct;
}

export type JobStatus = "PENDING" | "RUNNING" | "DONE" | "FAILED";

export interface ProcessResponse {
  job_id: string;
  status: JobStatus;
  filename: string;
  total_rows: number;
}

export interface JobStatusResponse {
  id: string;
  status: JobStatus;
  input_filename: string | null;
  total_rows: number;
  processed_rows: number;
  error: string | null;
  created_at: string;
  updated_at: string;
}

export interface JobResultsResponse {
  job_id: string;
  count: number;
  products: ProductRow[];
}

export type MatchStatus = "EXACT" | "CLOSE" | "MISS" | "NO_GT";

export interface AttributeEvalRow {
  label: string;
  got: string | null;
  expected: string;
  status: MatchStatus;
}

export interface ProductEvalRow {
  product_id: string;
  mfg_part_num: string;
  classpath: string | null;
  classpath_match: boolean;
  manufacturer_match: boolean;
  attribute_field_correct: number;
  attribute_field_total: number;
  attribute_rows: AttributeEvalRow[];
  description_status: Record<string, MatchStatus>;
}

export interface AccuracyStat {
  correct: number;
  total: number;
  pct: number;
}

export interface EvalSummary {
  rows_evaluated: number;
  classpath_accuracy: AccuracyStat;
  manufacturer_accuracy: AccuracyStat;
  attribute_accuracy: AccuracyStat;
  description_match_rates: Record<string, AccuracyStat>;
}

export interface EvaluateResponse {
  job_id: string;
  rows_total: number;
  rows_unscored: number;
  summary: EvalSummary | null;
  rows: ProductEvalRow[];
  note?: string;
}

async function asJson<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const detail = await res.text().catch(() => "");
    throw new Error(`${res.status} ${res.statusText}${detail ? `: ${detail}` : ""}`);
  }
  return res.json() as Promise<T>;
}

export async function processFile(file: File): Promise<ProcessResponse> {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch(`${API_BASE}/api/process`, { method: "POST", body: form });
  return asJson<ProcessResponse>(res);
}

export async function getJob(jobId: string): Promise<JobStatusResponse> {
  const res = await fetch(`${API_BASE}/api/job/${jobId}`);
  return asJson<JobStatusResponse>(res);
}

export async function getJobResults(jobId: string): Promise<JobResultsResponse> {
  const res = await fetch(`${API_BASE}/api/job/${jobId}/results`);
  return asJson<JobResultsResponse>(res);
}

export async function getProduct(productId: string): Promise<ProductDetail> {
  const res = await fetch(`${API_BASE}/api/product/${productId}`);
  return asJson<ProductDetail>(res);
}

export async function getEvaluation(jobId: string): Promise<EvaluateResponse> {
  const res = await fetch(`${API_BASE}/api/evaluate/${jobId}`);
  return asJson<EvaluateResponse>(res);
}
