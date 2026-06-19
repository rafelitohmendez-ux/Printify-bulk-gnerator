const Section = ({ label, children, testId }) => (
  <div className="flex flex-col gap-2" data-testid={testId}>
    <div className="text-[10px] uppercase tracking-[0.3em] text-zinc-500 font-body border-b border-zinc-800 pb-2">
      {label}
    </div>
    {children}
  </div>
);

const SeoPanel = ({ capsule }) => {
  if (!capsule) {
    return (
      <div className="p-6 text-xs text-zinc-500 font-body uppercase tracking-widest">
        // Awaiting capsule signal_
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-6 p-6 lg:p-8" data-testid="seo-panel">
      <Section label="// Capsule Name" testId="seo-capsule-name">
        <h2 className="font-heading text-3xl lg:text-4xl uppercase tracking-tight text-white leading-none">
          {capsule.capsule_name}
        </h2>
      </Section>

      <Section label="// SEO Title (Keyword Formula)" testId="seo-title">
        <p className="text-sm text-zinc-200 font-body leading-relaxed">
          {capsule.title}
        </p>
      </Section>

      <Section label="// THE GRIND - Description Template" testId="seo-description">
        <pre className="text-xs text-zinc-300 font-body leading-relaxed whitespace-pre-wrap border-l-2 border-zinc-700 pl-4 max-h-72 overflow-y-auto">
          {capsule.description}
        </pre>
      </Section>

      <Section label={`// Tag Pool (${capsule.tags?.length || 0}/13)`} testId="seo-tags-list">
        <div className="flex flex-wrap gap-2">
          {(capsule.tags || []).map((tag, i) => (
            <span
              key={i}
              data-testid={`tag-${i}`}
              className="px-3 py-1 bg-zinc-900 border border-zinc-700 text-[11px] text-zinc-300 font-body uppercase tracking-wider hover:bg-zinc-800 hover:text-white transition-colors"
            >
              {tag}
            </span>
          ))}
        </div>
      </Section>

      <Section label="// Print Concepts">
        <div className="grid grid-cols-1 gap-3 text-xs text-zinc-400 font-body">
          <div className="border border-zinc-800 p-3">
            <div className="text-[10px] text-zinc-500 uppercase tracking-widest mb-1">
              Front
            </div>
            <p data-testid="front-concept" className="text-zinc-200">{capsule.front_concept}</p>
          </div>
          <div className="border border-zinc-800 p-3">
            <div className="text-[10px] text-zinc-500 uppercase tracking-widest mb-1">
              Back
            </div>
            <p data-testid="back-concept" className="text-zinc-200">{capsule.back_concept}</p>
          </div>
        </div>
      </Section>
    </div>
  );
};

export default SeoPanel;
