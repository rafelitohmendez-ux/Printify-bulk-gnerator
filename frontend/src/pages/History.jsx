import { useEffect, useState } from "react";
import { toast } from "sonner";
import { DownloadSimple, Archive, Storefront, ArrowUpRight, ArrowsClockwise } from "@phosphor-icons/react";
import { listApproved, exportCsvUrl, imageUrl, pushToPrintify } from "@/lib/api";

const PrintifyBadge = ({ status, productId, error, onPush, pushing }) => {
  if (status === "success") {
    return (
      <a
        href={`https://printify.com/app/editor/${productId}`}
        target="_blank"
        rel="noopener noreferrer"
        data-testid={`printify-link-${productId}`}
        className="flex items-center gap-1 px-2 py-1 bg-green-950 border border-green-900/60 text-[10px] text-green-300 uppercase tracking-wider hover:bg-green-900/40 transition-colors"
      >
        <Storefront size={10} weight="bold" />
        On Printify
        <ArrowUpRight size={10} weight="bold" />
      </a>
    );
  }
  if (status === "failed") {
    return (
      <button
        onClick={onPush}
        disabled={pushing}
        title={error || "push failed"}
        data-testid="printify-retry"
        className="flex items-center gap-1 px-2 py-1 bg-red-950/60 border border-red-900/60 text-[10px] text-red-300 uppercase tracking-wider hover:bg-red-900/40 transition-colors disabled:opacity-50"
      >
        <ArrowsClockwise size={10} weight="bold" className={pushing ? "animate-spin" : ""} />
        {pushing ? "..." : "Retry Push"}
      </button>
    );
  }
  return (
    <button
      onClick={onPush}
      disabled={pushing}
      data-testid="printify-push-btn"
      className="flex items-center gap-1 px-2 py-1 bg-zinc-900 border border-zinc-700 text-[10px] text-zinc-300 uppercase tracking-wider hover:bg-zinc-800 hover:text-white transition-colors disabled:opacity-50"
    >
      <Storefront size={10} weight="bold" />
      {pushing ? "pushing..." : "Push"}
    </button>
  );
};

const History = () => {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [pushingId, setPushingId] = useState(null);

  const reload = async () => {
    const data = await listApproved();
    setItems(data);
  };

  useEffect(() => {
    (async () => {
      try {
        await reload();
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  const onPush = async (id) => {
    setPushingId(id);
    try {
      await pushToPrintify(id);
      toast.success("Pushed to Printify as draft");
      await reload();
    } catch (e) {
      toast.error("Push failed", { description: e?.response?.data?.detail || e.message });
      await reload();
    } finally {
      setPushingId(null);
    }
  };

  return (
    <main className="relative grunge-overlay min-h-[calc(100vh-80px)]" data-testid="history-page">
      <div className="flex items-center justify-between px-6 lg:px-10 py-6 border-b border-zinc-800 bg-zinc-950">
        <div className="flex items-center gap-4">
          <Archive size={28} weight="bold" className="text-zinc-400" />
          <div>
            <h1 className="font-heading text-3xl uppercase tracking-tight text-white leading-none">
              Approved Archive
            </h1>
            <p className="text-[10px] text-zinc-500 uppercase tracking-[0.3em] mt-1">
              // {items.length} capsule{items.length === 1 ? "" : "s"} cleared for production
            </p>
          </div>
        </div>
        <a
          data-testid="history-export-btn"
          href={exportCsvUrl()}
          className="flex items-center gap-2 px-4 py-3 bg-white text-black font-heading uppercase text-sm tracking-[0.2em] hover:bg-zinc-200 transition-colors"
        >
          <DownloadSimple size={18} weight="bold" />
          Export CSV
        </a>
      </div>

      {loading ? (
        <div className="p-10 text-xs text-zinc-500 font-body uppercase tracking-widest">
          // Loading archive<span className="blink">_</span>
        </div>
      ) : items.length === 0 ? (
        <div className="p-10 text-center text-zinc-500">
          <p className="font-heading text-2xl uppercase tracking-tight">
            No approved capsules yet
          </p>
          <p className="text-[11px] uppercase tracking-[0.3em] mt-2">
            // start grinding from the generator
          </p>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-px bg-zinc-800 border-t border-zinc-800">
          {items.map((c) => (
            <article
              key={c.id}
              data-testid={`archive-item-${c.id}`}
              className="bg-zinc-950 p-6 flex flex-col gap-4"
            >
              <div className="grid grid-cols-2 gap-2">
                <div className="aspect-square bg-black border border-zinc-800 flex items-center justify-center overflow-hidden">
                  <img
                    src={imageUrl(c.id, "front")}
                    alt="front"
                    className="w-1/3 h-1/3 object-contain mix-blend-screen"
                  />
                </div>
                <div className="aspect-square bg-black border border-zinc-800 flex items-center justify-center overflow-hidden">
                  <img
                    src={imageUrl(c.id, "back")}
                    alt="back"
                    className="w-4/5 h-4/5 object-contain mix-blend-screen"
                  />
                </div>
              </div>
              <div>
                <h3 className="font-heading text-xl uppercase tracking-tight text-white leading-none">
                  {c.capsule_name}
                </h3>
                <p className="text-[10px] text-zinc-500 font-body mt-2 uppercase tracking-widest">
                  {c.approved_at ? new Date(c.approved_at).toLocaleString() : ""}
                </p>
              </div>
              <p className="text-xs text-zinc-400 line-clamp-3">{c.title}</p>
              <div className="flex items-center justify-between gap-2 flex-wrap">
                <div className="flex flex-wrap gap-1 flex-1 min-w-0">
                  {(c.tags || []).slice(0, 5).map((tag, i) => (
                    <span
                      key={i}
                      className="px-2 py-0.5 bg-zinc-900 border border-zinc-800 text-[10px] text-zinc-400 uppercase tracking-wider"
                    >
                      {tag}
                    </span>
                  ))}
                  {c.tags && c.tags.length > 5 && (
                    <span className="px-2 py-0.5 text-[10px] text-zinc-600 uppercase tracking-wider">
                      +{c.tags.length - 5}
                    </span>
                  )}
                </div>
                <PrintifyBadge
                  status={c.printify_push_status}
                  productId={c.printify_product_id}
                  error={c.printify_push_error}
                  onPush={() => onPush(c.id)}
                  pushing={pushingId === c.id}
                />
              </div>
            </article>
          ))}
        </div>
      )}
    </main>
  );
};

export default History;
