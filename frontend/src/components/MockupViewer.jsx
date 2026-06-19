import { useState } from "react";
import { ArrowsClockwise } from "@phosphor-icons/react";
import { imageUrl } from "@/lib/api";

const Tee = ({ children, label, testId, onRegen, regenerating, designUrl, designTestId }) => (
  <div className="flex flex-col items-center gap-3 w-full" data-testid={testId}>
    <div className="flex items-center justify-between w-full max-w-[360px]">
      <span className="text-[10px] uppercase tracking-[0.3em] text-zinc-500 font-body">
        {label}
      </span>
      {onRegen && (
        <button
          data-testid={`${testId}-regen`}
          onClick={onRegen}
          disabled={regenerating}
          className="text-[10px] uppercase tracking-[0.2em] text-zinc-500 hover:text-white border border-zinc-800 hover:border-zinc-600 px-2 py-1 flex items-center gap-1 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
          title="Regenerate this side"
        >
          <ArrowsClockwise
            size={10}
            weight="bold"
            className={regenerating ? "animate-spin" : ""}
          />
          {regenerating ? "..." : "Regen"}
        </button>
      )}
    </div>
    <div className="relative w-full max-w-[360px] aspect-[3/4] bg-black border border-zinc-800 flex items-center justify-center overflow-hidden">
      <svg
        viewBox="0 0 300 400"
        className="absolute inset-0 w-full h-full opacity-100"
        fill="none"
      >
        <path
          d="M40 60 L100 30 Q150 50 200 30 L260 60 L290 110 L240 130 L240 380 L60 380 L60 130 L10 110 Z"
          fill="#000000"
          stroke="#1f1f23"
          strokeWidth="1.5"
        />
        <path d="M110 35 Q150 65 190 35" stroke="#1f1f23" strokeWidth="1.5" fill="none" />
      </svg>
      <div className="relative z-10 w-full h-full">{children}</div>
    </div>
    {designUrl !== undefined && (
      <div className="w-full max-w-[360px] flex flex-col gap-1.5">
        <span className="text-[10px] uppercase tracking-[0.3em] text-zinc-600 font-body">
          // as-printed graphic
        </span>
        <div className="bg-black border border-zinc-800 aspect-square flex items-center justify-center overflow-hidden">
          {designUrl ? (
            <img
              src={designUrl}
              alt="raw design"
              data-testid={designTestId}
              className="w-full h-full object-contain"
            />
          ) : (
            <span className="text-[9px] text-zinc-700 uppercase tracking-widest">
              {regenerating ? "rerolling..." : "—"}
            </span>
          )}
        </div>
      </div>
    )}
  </div>
);

const Placeholder = ({ cls, hint }) => (
  <div
    className={`absolute ${cls} border border-dashed border-zinc-700 flex items-center justify-center text-[9px] text-zinc-600 font-body uppercase tracking-widest`}
  >
    {hint}
  </div>
);

const MockupViewer = ({ capsule, onRegenerate }) => {
  const [regenSide, setRegenSide] = useState(null);
  const [bust, setBust] = useState({ front: 0, back: 0 });

  if (!capsule?.id) {
    return (
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-8 p-8 lg:p-12 w-full">
        <Tee label="// Front - Left-Chest Print" testId="mockup-viewer-front" designUrl={null}>
          <Placeholder cls="top-[18%] left-[28%] w-[60px] h-[60px]" hint="FRONT" />
        </Tee>
        <Tee label="// Back - Oversized Print" testId="mockup-viewer-back" designUrl={null}>
          <Placeholder cls="top-[14%] left-[14%] right-[14%] bottom-[18%]" hint="BACK" />
        </Tee>
      </div>
    );
  }

  const onRegen = async (side) => {
    if (regenSide) return;
    setRegenSide(side);
    try {
      await onRegenerate(side);
      setBust((b) => ({ ...b, [side]: Date.now() }));
    } finally {
      setRegenSide(null);
    }
  };

  const front = imageUrl(capsule.id, "front", bust.front || undefined);
  const back = imageUrl(capsule.id, "back", bust.back || undefined);

  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-8 p-8 lg:p-12 w-full">
      <Tee
        label="// Front - Left-Chest Print"
        testId="mockup-viewer-front"
        onRegen={() => onRegen("front")}
        regenerating={regenSide === "front"}
        designUrl={regenSide === "front" ? null : front}
        designTestId="raw-front-design"
      >
        {regenSide === "front" ? (
          <Placeholder cls="top-[18%] left-[28%] w-[60px] h-[60px]" hint="rerolling..." />
        ) : (
          <img
            src={front}
            alt="front print"
            data-testid="mockup-front-img"
            className="absolute top-[18%] left-[28%] w-[60px] h-[60px] object-contain mix-blend-screen"
          />
        )}
      </Tee>
      <Tee
        label="// Back - Oversized Print"
        testId="mockup-viewer-back"
        onRegen={() => onRegen("back")}
        regenerating={regenSide === "back"}
        designUrl={regenSide === "back" ? null : back}
        designTestId="raw-back-design"
      >
        {regenSide === "back" ? (
          <Placeholder
            cls="top-[14%] left-[14%] right-[14%] bottom-[18%]"
            hint="rerolling..."
          />
        ) : (
          <img
            src={back}
            alt="back print"
            data-testid="mockup-back-img"
            className="absolute top-[14%] left-[14%] right-[14%] bottom-[18%] w-[72%] h-[68%] object-contain mix-blend-screen"
          />
        )}
      </Tee>
    </div>
  );
};

export default MockupViewer;
