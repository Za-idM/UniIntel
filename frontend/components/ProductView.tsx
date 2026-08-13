"use client";

import { useEffect, useState } from "react";
import { getProduct, type ProductDetail, type ProductRow } from "@/lib/api";
import { ConfidenceBadge } from "@/components/Badges";
import AttributeRow from "@/components/AttributeRow";
import DescriptionTabs from "@/components/DescriptionTabs";

export default function ProductView({
  productId,
  jobProducts,
  onSelectProduct,
}: {
  productId: string | null;
  jobProducts: ProductRow[];
  onSelectProduct: (id: string) => void;
}) {
  const [product, setProduct] = useState<ProductDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!productId) {
      setProduct(null);
      return;
    }
    setLoading(true);
    setError(null);
    getProduct(productId)
      .then(setProduct)
      .catch((err) => setError(err instanceof Error ? err.message : "failed to load product"))
      .finally(() => setLoading(false));
  }, [productId]);

  if (!productId) {
    return (
      <div className="mx-auto max-w-5xl px-6 py-10 text-left">
        <p className="text-sm text-ink-500">
          No product selected. Process a file on the Upload tab, then pick a row to inspect.
        </p>
      </div>
    );
  }

  if (loading || !product) {
    return (
      <div className="mx-auto max-w-5xl px-6 py-10 text-left">
        <p className="text-sm text-ink-500">{error || "Loading…"}</p>
      </div>
    );
  }

  const data = product.data;
  const filledCount = data.attributes.filter((a) => a.value).length;

  return (
    <div className="mx-auto max-w-5xl px-6 py-5">
      {jobProducts.length > 1 && (
        <div className="mb-3 flex flex-wrap gap-1">
          {jobProducts.map((p) => (
            <button
              key={p.id}
              onClick={() => onSelectProduct(p.id)}
              className={`rounded border px-1.5 py-0.5 font-mono text-[11px] transition-colors ${
                p.id === productId
                  ? "border-accent-600 bg-accent-50 text-accent-700"
                  : "border-paper-300 bg-white text-ink-500 hover:border-accent-600 hover:text-accent-700"
              }`}
            >
              {p.mfg_part_num}
            </button>
          ))}
        </div>
      )}

      {/* Header block */}
      <div className="mb-3 rounded border border-paper-300 bg-white px-4 py-3">
        <div className="flex items-start justify-between">
          <div>
            <div className="font-mono text-base font-semibold text-ink900">{data.mfg_part_num}</div>
            <div className="mt-0.5 text-[12.5px] text-ink-500">{data.part_desc}</div>
          </div>
          <ConfidenceBadge band={data.confidence_band} score={data.confidence} />
        </div>
        <div className="mt-3 grid grid-cols-3 gap-4 border-t border-paper-200 pt-2.5 text-[13px]">
          <div>
            <div className="font-mono text-[10px] font-semibold uppercase tracking-wide text-ink-400">
              Manufacturer
            </div>
            <div className="mt-0.5 text-ink900">
              {data.manufacturer_name || <span className="text-status-miss">unresolved</span>}
            </div>
            {data.part_manuf_raw && (
              <div className="mt-0.5 font-mono text-[11px] text-ink-400">from &ldquo;{data.part_manuf_raw}&rdquo;</div>
            )}
          </div>
          <div>
            <div className="font-mono text-[10px] font-semibold uppercase tracking-wide text-ink-400">Brand</div>
            <div className="mt-0.5 text-ink900">{data.brand_name || <span className="text-ink-400">&mdash;</span>}</div>
          </div>
          <div>
            <div className="font-mono text-[10px] font-semibold uppercase tracking-wide text-ink-400">
              Classpath
            </div>
            <div className="mt-0.5 text-ink900">
              {data.classpath || <span className="text-status-miss">unclassified</span>}
            </div>
          </div>
        </div>
      </div>

      {/* Attributes */}
      <div className="mb-3">
        <div className="mb-1.5 flex items-baseline justify-between">
          <h2 className="font-mono text-[11px] font-semibold uppercase tracking-wide text-ink-500">
            attributes &middot; {filledCount}/{data.attributes.length} filled
          </h2>
          <span className="text-[11px] text-ink-400">click a row for source evidence</span>
        </div>
        <div className="overflow-hidden rounded border border-paper-300 bg-white">
          <table className="w-full border-collapse text-left text-[13px]">
            <thead>
              <tr className="border-b border-paper-300 bg-paper-100 text-ink-500">
                <th className="w-8 px-3 py-1.5" />
                <th className="px-3 py-1.5 font-mono text-[11px] font-semibold uppercase tracking-wide">
                  Attribute
                </th>
                <th className="px-3 py-1.5 font-mono text-[11px] font-semibold uppercase tracking-wide">Value</th>
                <th className="px-3 py-1.5 font-mono text-[11px] font-semibold uppercase tracking-wide">Origin</th>
              </tr>
            </thead>
            <tbody>
              {data.attributes.map((attr) => (
                <AttributeRow key={attr.slot} attr={attr} />
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* Descriptions */}
      <div className="rounded border border-paper-300 bg-white px-4 py-2.5">
        <h2 className="mb-0.5 font-mono text-[11px] font-semibold uppercase tracking-wide text-ink-500">
          generated descriptions
        </h2>
        <DescriptionTabs descriptions={data.descriptions} />
      </div>
    </div>
  );
}
