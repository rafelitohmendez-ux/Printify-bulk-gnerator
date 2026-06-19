import { useEffect, useState } from "react";
import { DownloadSimple, Archive } from "@phosphor-icons/react";
import { listApproved, exportCsvUrl, imageUrl } from "@/lib/api";

const History = () => {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    (async () => {
      try {
        const data = await listApproved();
        setItems(data);
      } finally {
        setLoading(false);
      }
    })();
  }, []);

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
              <div className="flex flex-wrap gap-1">
                {(c.tags || []).slice(0, 6).map((tag, i) => (
                  <span
                    key={i}
                    className="px-2 py-0.5 bg-zinc-900 border border-zinc-800 text-[10px] text-zinc-400 uppercase tracking-wider"
                  >
                    {tag}
                  </span>
                ))}
                {c.tags && c.tags.length > 6 && (
                  <span className="px-2 py-0.5 text-[10px] text-zinc-600 uppercase tracking-wider">
                    +{c.tags.length - 6}
                  </span>
                )}
              </div>
            </article>
          ))}
        </div>
      )}
    </main>
  );
};

export default History;
