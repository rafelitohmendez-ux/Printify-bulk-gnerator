import { imageUrl } from "@/lib/api";

const Tee = ({ children, label, testId }) => (
  <div className="flex flex-col items-center gap-3 w-full" data-testid={testId}>
    <span className="text-[10px] uppercase tracking-[0.3em] text-zinc-500 font-body">
      {label}
    </span>
    {/* Stylised tee silhouette: pure black with hint of outline */}
    <div className="relative w-full max-w-[360px] aspect-[3/4] bg-black border border-zinc-800 flex items-center justify-center overflow-hidden">
      {/* shoulders/sleeves hint via clip-path */}
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
        {/* collar */}
        <path d="M110 35 Q150 65 190 35" stroke="#1f1f23" strokeWidth="1.5" fill="none" />
      </svg>
      <div className="relative z-10 w-full h-full">{children}</div>
    </div>
  </div>
);

const ImagePlaceholder = ({ size, hint }) => (
  <div
    className={`absolute ${size} border border-dashed border-zinc-700 flex items-center justify-center text-[9px] text-zinc-600 font-body uppercase tracking-widest`}
  >
    {hint}
  </div>
);

const MockupViewer = ({ capsule, loading }) => {
  // Render front print at top-left chest area; back print as large overlay centered
  const front = capsule?.id ? imageUrl(capsule.id, "front") : null;
  const back = capsule?.id ? imageUrl(capsule.id, "back") : null;

  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-8 p-8 lg:p-12 w-full">
      <Tee label="// Front - Left-Chest Print" testId="mockup-viewer-front">
        {loading ? (
          <ImagePlaceholder
            size="top-[18%] left-[28%] w-[60px] h-[60px]"
            hint="generating..."
          />
        ) : front ? (
          <img
            src={front}
            alt="front print"
            data-testid="mockup-front-img"
            className="absolute top-[18%] left-[28%] w-[60px] h-[60px] object-contain mix-blend-screen"
          />
        ) : (
          <ImagePlaceholder
            size="top-[18%] left-[28%] w-[60px] h-[60px]"
            hint="FRONT"
          />
        )}
      </Tee>
      <Tee label="// Back - Oversized Print" testId="mockup-viewer-back">
        {loading ? (
          <ImagePlaceholder
            size="top-[14%] left-[14%] right-[14%] bottom-[18%]"
            hint="generating..."
          />
        ) : back ? (
          <img
            src={back}
            alt="back print"
            data-testid="mockup-back-img"
            className="absolute top-[14%] left-[14%] right-[14%] bottom-[18%] w-[72%] h-[68%] object-contain mix-blend-screen"
          />
        ) : (
          <ImagePlaceholder
            size="top-[14%] left-[14%] right-[14%] bottom-[18%]"
            hint="BACK"
          />
        )}
      </Tee>
    </div>
  );
};

export default MockupViewer;
