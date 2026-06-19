import { useEffect, useState, useCallback, useRef } from "react";
import { toast } from "sonner";
import {
  CheckSquareOffset,
  XSquare,
  ArrowsClockwise,
  Gear,
  Stack,
} from "@phosphor-icons/react";
import {
  nextCapsule,
  approveCapsule,
  denyCapsule,
  fetchStats,
  queueStatus,
  regenerateImage,
} from "@/lib/api";
import MockupViewer from "@/components/MockupViewer";
import SeoPanel from "@/components/SeoPanel";
import SettingsModal from "@/components/SettingsModal";

const StatItem = ({ label, value, testId }) => (
  <div className="flex flex-col" data-testid={testId}>
    <span className="text-[10px] text-zinc-500 font-body uppercase tracking-[0.3em]">
      {label}
    </span>
    <span className="text-2xl text-zinc-100 font-heading leading-none mt-1">
      {value}
    </span>
  </div>
);

const LoadingState = ({ note }) => (
  <div className="flex flex-col items-center justify-center min-h-[60vh] gap-6 text-zinc-500">
    <div className="relative w-32 h-32 border border-zinc-800 overflow-hidden bg-black">
      <div className="scanline" />
      <div className="absolute inset-0 flex items-center justify-center">
        <ArrowsClockwise size={36} className="animate-spin text-zinc-600" weight="bold" />
      </div>
    </div>
    <div className="text-center">
      <p className="font-heading text-2xl uppercase tracking-tight text-zinc-300">
        {note || "Forging Capsule"}
      </p>
      <p className="text-[11px] uppercase tracking-[0.3em] font-body mt-2">
        gemini engine // ink rendering<span className="blink">_</span>
      </p>
    </div>
  </div>
);

const Dashboard = () => {
  const [capsule, setCapsule] = useState(null);
  const [edits, setEdits] = useState({});
  const [loading, setLoading] = useState(false);
  const [acting, setActing] = useState(false);
  const [counts, setCounts] = useState({ approved: 0, denied: 0, reviewed: 0 });
  const [queue, setQueue] = useState({ depth: 0, target: 5 });
  const [showSettings, setShowSettings] = useState(false);
  const pollRef = useRef(null);

  const loadStats = useCallback(async () => {
    try {
      const s = await fetchStats();
      setCounts((c) => ({ ...c, approved: s.approved }));
    } catch {
      /* non-critical */
    }
  }, []);

  const pollQueue = useCallback(async () => {
    try {
      const q = await queueStatus();
      setQueue(q);
    } catch {
      /* non-critical */
    }
  }, []);

  const loadNext = useCallback(async () => {
    setCapsule(null);
    setEdits({});
    setLoading(true);
    try {
      const c = await nextCapsule();
      setCapsule(c);
    } catch (e) {
      console.error(e);
      toast.error("Load next failed", {
        description: e?.response?.data?.detail || e.message,
      });
    } finally {
      setLoading(false);
      pollQueue();
    }
  }, [pollQueue]);

  useEffect(() => {
    loadStats();
    loadNext();
    pollRef.current = setInterval(pollQueue, 3000);
    return () => clearInterval(pollRef.current);
  }, [loadNext, loadStats, pollQueue]);

  const onApprove = async () => {
    if (!capsule || acting) return;
    setActing(true);
    try {
      const payload = {};
      if (edits.title !== undefined) payload.title = edits.title;
      if (edits.capsule_name !== undefined) payload.capsule_name = edits.capsule_name;
      if (edits.tags !== undefined) payload.tags = edits.tags;
      await approveCapsule(capsule.id, payload);
      toast.success(`Approved // ${edits.capsule_name ?? capsule.capsule_name}`);
      setCounts((c) => ({
        approved: c.approved + 1,
        denied: c.denied,
        reviewed: c.reviewed + 1,
      }));
      await loadNext();
    } catch (e) {
      toast.error("Approve failed", {
        description: e?.response?.data?.detail || e.message,
      });
    } finally {
      setActing(false);
    }
  };

  const onDeny = async () => {
    if (!capsule || acting) return;
    setActing(true);
    try {
      await denyCapsule(capsule.id);
      toast(`Denied // ${capsule.capsule_name}`);
      setCounts((c) => ({
        approved: c.approved,
        denied: c.denied + 1,
        reviewed: c.reviewed + 1,
      }));
      await loadNext();
    } catch (e) {
      toast.error("Deny failed", {
        description: e?.response?.data?.detail || e.message,
      });
    } finally {
      setActing(false);
    }
  };

  const onRegenerate = async (side) => {
    if (!capsule) return;
    try {
      const updated = await regenerateImage(capsule.id, side);
      setCapsule(updated);
      toast.success(`Regenerated ${side} graphic`);
    } catch (e) {
      toast.error("Regenerate failed", {
        description: e?.response?.data?.detail || e.message,
      });
      throw e;
    }
  };

  // Queue depth pip indicator
  const pips = Array.from({ length: Math.max(1, queue.target) }, (_, i) => i);

  return (
    <main className="relative grunge-overlay" data-testid="dashboard-page">
      {/* counter strip */}
      <div
        className="flex flex-wrap gap-x-8 gap-y-3 items-center px-6 lg:px-8 py-5 border-b border-zinc-800 bg-zinc-950 relative z-10"
        data-testid="counter-strip"
      >
        <StatItem label="Approved" value={counts.approved} testId="counter-approved" />
        <div className="w-px h-8 bg-zinc-800" />
        <StatItem label="Denied (session)" value={counts.denied} testId="counter-denied" />
        <div className="w-px h-8 bg-zinc-800" />
        <StatItem label="Reviewed (session)" value={counts.reviewed} testId="counter-reviewed" />
        <div className="w-px h-8 bg-zinc-800" />
        <div className="flex flex-col" data-testid="queue-indicator">
          <span className="text-[10px] text-zinc-500 font-body uppercase tracking-[0.3em] flex items-center gap-1.5">
            <Stack size={10} weight="bold" />
            Queue
          </span>
          <div className="flex gap-1 mt-1.5 items-center">
            {pips.map((i) => (
              <span
                key={i}
                className={`w-2 h-4 ${i < queue.depth ? "bg-white" : "bg-zinc-800 border border-zinc-700"}`}
              />
            ))}
            <span className="ml-2 text-[10px] text-zinc-500 font-body">
              {queue.depth}/{queue.target}
            </span>
          </div>
        </div>
        <div className="flex-1" />
        <button
          data-testid="open-settings"
          onClick={() => setShowSettings(true)}
          className="flex items-center gap-2 text-[11px] uppercase tracking-[0.25em] font-body text-zinc-400 hover:text-white border border-zinc-800 hover:border-zinc-600 px-3 py-2 transition-colors"
        >
          <Gear size={14} weight="bold" />
          Settings
        </button>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-12 min-h-[calc(100vh-200px)]">
        <section
          className="col-span-1 lg:col-span-8 bg-black border-r border-zinc-800 flex items-center justify-center relative overflow-hidden"
          data-testid="mockup-section"
        >
          {loading || !capsule ? (
            <LoadingState note={queue.depth > 0 ? "Loading from queue" : "Forging Capsule"} />
          ) : (
            <MockupViewer capsule={capsule} onRegenerate={onRegenerate} />
          )}
        </section>

        <aside
          className="col-span-1 lg:col-span-4 bg-zinc-950 overflow-y-auto max-h-[calc(100vh-260px)] relative"
          data-testid="seo-section"
        >
          {loading || !capsule ? (
            <div className="p-8 text-xs text-zinc-500 font-body uppercase tracking-widest">
              // Drafting SEO payload<span className="blink">_</span>
            </div>
          ) : (
            <SeoPanel capsule={capsule} edits={edits} onChangeEdits={setEdits} />
          )}
        </aside>
      </div>

      <div className="grid grid-cols-2 border-t border-zinc-800 relative z-10">
        <button
          data-testid="deny-button"
          onClick={onDeny}
          disabled={loading || acting || !capsule}
          className="h-24 lg:h-28 bg-[#8b0000] text-white font-heading text-2xl lg:text-4xl uppercase tracking-[0.2em] hover:bg-[#660000] transition-colors duration-75 flex items-center justify-center gap-4 disabled:opacity-40 disabled:cursor-not-allowed border-r border-zinc-800"
        >
          <XSquare size={36} weight="bold" />
          Deny
        </button>
        <button
          data-testid="approve-button"
          onClick={onApprove}
          disabled={loading || acting || !capsule}
          className="h-24 lg:h-28 bg-white text-black font-heading text-2xl lg:text-4xl uppercase tracking-[0.2em] hover:bg-zinc-200 transition-colors duration-75 flex items-center justify-center gap-4 disabled:opacity-40 disabled:cursor-not-allowed"
        >
          <CheckSquareOffset size={36} weight="bold" />
          Approve
        </button>
      </div>

      <SettingsModal
        open={showSettings}
        onClose={() => setShowSettings(false)}
        onSaved={() => {
          // queue was flushed server-side; refresh status
          pollQueue();
        }}
      />
    </main>
  );
};

export default Dashboard;
