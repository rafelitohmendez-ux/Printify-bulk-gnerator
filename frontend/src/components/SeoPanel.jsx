import { useState } from "react";
import { X, Plus, PencilSimple, Check } from "@phosphor-icons/react";

const Section = ({ label, children, testId, action }) => (
  <div className="flex flex-col gap-2" data-testid={testId}>
    <div className="flex items-center justify-between text-[10px] uppercase tracking-[0.3em] text-zinc-500 font-body border-b border-zinc-800 pb-2">
      <span>{label}</span>
      {action}
    </div>
    {children}
  </div>
);

const SeoPanel = ({ capsule, edits, onChangeEdits }) => {
  const [editingTitle, setEditingTitle] = useState(false);
  const [editingName, setEditingName] = useState(false);
  const [newTagInput, setNewTagInput] = useState("");

  if (!capsule) {
    return (
      <div className="p-6 text-xs text-zinc-500 font-body uppercase tracking-widest">
        // Awaiting capsule signal_
      </div>
    );
  }

  const title = edits.title ?? capsule.title;
  const name = edits.capsule_name ?? capsule.capsule_name;
  const tags = edits.tags ?? capsule.tags ?? [];

  const setTitle = (v) => onChangeEdits({ ...edits, title: v });
  const setName = (v) => onChangeEdits({ ...edits, capsule_name: v });
  const setTags = (v) => onChangeEdits({ ...edits, tags: v });

  const removeTag = (i) => setTags(tags.filter((_, idx) => idx !== i));
  const addTag = () => {
    const t = newTagInput.trim();
    if (!t || tags.length >= 13) return;
    if (tags.some((x) => x.toLowerCase() === t.toLowerCase())) return;
    setTags([...tags, t]);
    setNewTagInput("");
  };

  return (
    <div className="flex flex-col gap-6 p-6 lg:p-8" data-testid="seo-panel">
      <Section
        label="// Capsule Name"
        testId="seo-capsule-name"
        action={
          <button
            data-testid="edit-name-btn"
            onClick={() => setEditingName(!editingName)}
            className="text-zinc-500 hover:text-white transition-colors p-1"
            aria-label="edit name"
          >
            {editingName ? <Check size={12} weight="bold" /> : <PencilSimple size={12} weight="bold" />}
          </button>
        }
      >
        {editingName ? (
          <input
            data-testid="capsule-name-input"
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            onBlur={() => setEditingName(false)}
            autoFocus
            className="font-heading text-3xl lg:text-4xl uppercase tracking-tight text-white leading-none bg-transparent border-b border-zinc-700 focus:border-white outline-none w-full"
          />
        ) : (
          <h2 className="font-heading text-3xl lg:text-4xl uppercase tracking-tight text-white leading-none">
            {name}
          </h2>
        )}
      </Section>

      <Section
        label="// SEO Title (Keyword Formula)"
        testId="seo-title"
        action={
          <button
            data-testid="edit-title-btn"
            onClick={() => setEditingTitle(!editingTitle)}
            className="text-zinc-500 hover:text-white transition-colors p-1"
            aria-label="edit title"
          >
            {editingTitle ? <Check size={12} weight="bold" /> : <PencilSimple size={12} weight="bold" />}
          </button>
        }
      >
        {editingTitle ? (
          <textarea
            data-testid="seo-title-input"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            rows={3}
            autoFocus
            className="text-sm text-zinc-100 font-body leading-relaxed bg-zinc-900 border border-zinc-700 focus:border-white outline-none p-2 resize-none w-full"
          />
        ) : (
          <p className="text-sm text-zinc-200 font-body leading-relaxed break-words">{title}</p>
        )}
      </Section>

      <Section label="// THE GRIND - Description Template" testId="seo-description">
        <pre className="text-xs text-zinc-300 font-body leading-relaxed whitespace-pre-wrap border-l-2 border-zinc-700 pl-4 max-h-72 overflow-y-auto">
          {capsule.description}
        </pre>
      </Section>

      <Section label={`// Tag Pool (${tags.length}/13)`} testId="seo-tags-list">
        <div className="flex flex-wrap gap-2 mb-2">
          {tags.map((tag, i) => (
            <span
              key={`${tag}-${i}`}
              data-testid={`tag-${i}`}
              className="group px-3 py-1 bg-zinc-900 border border-zinc-700 text-[11px] text-zinc-300 font-body uppercase tracking-wider flex items-center gap-2"
            >
              {tag}
              <button
                onClick={() => removeTag(i)}
                className="text-zinc-600 hover:text-red-400 transition-colors"
                aria-label={`remove ${tag}`}
              >
                <X size={10} weight="bold" />
              </button>
            </span>
          ))}
        </div>
        {tags.length < 13 && (
          <div className="flex gap-2">
            <input
              data-testid="add-tag-input"
              type="text"
              placeholder="+ add tag"
              value={newTagInput}
              onChange={(e) => setNewTagInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  addTag();
                }
              }}
              className="flex-1 bg-zinc-900 border border-zinc-700 px-2 py-1 text-[11px] text-zinc-200 font-body focus:outline-none focus:border-white uppercase tracking-wider"
            />
            <button
              data-testid="add-tag-btn"
              onClick={addTag}
              className="px-2 py-1 border border-zinc-700 text-zinc-400 hover:bg-zinc-800 hover:text-white transition-colors"
            >
              <Plus size={12} weight="bold" />
            </button>
          </div>
        )}
      </Section>

      <Section label="// Print Concepts">
        <div className="grid grid-cols-1 gap-3 text-xs text-zinc-400 font-body">
          <div className="border border-zinc-800 p-3">
            <div className="text-[10px] text-zinc-500 uppercase tracking-widest mb-1">Front</div>
            <p data-testid="front-concept" className="text-zinc-200">
              {capsule.front_concept}
            </p>
          </div>
          <div className="border border-zinc-800 p-3">
            <div className="text-[10px] text-zinc-500 uppercase tracking-widest mb-1">Back</div>
            <p data-testid="back-concept" className="text-zinc-200">
              {capsule.back_concept}
            </p>
          </div>
          {capsule.theme_seed && (
            <div className="border border-zinc-800 p-3">
              <div className="text-[10px] text-zinc-500 uppercase tracking-widest mb-1">
                Theme Seed
              </div>
              <p data-testid="theme-seed" className="text-zinc-300">
                {capsule.theme_seed}
              </p>
            </div>
          )}
        </div>
      </Section>
    </div>
  );
};

export default SeoPanel;
