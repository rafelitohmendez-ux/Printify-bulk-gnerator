import { useEffect, useState } from "react";
import { toast } from "sonner";
import { X, Plus, FloppyDisk, Tag, Stack } from "@phosphor-icons/react";
import { getSettings, updateSettings } from "@/lib/api";

const SectionTitle = ({ icon, children }) => (
  <div className="flex items-center gap-2 text-[11px] uppercase tracking-[0.3em] text-zinc-500 font-body border-b border-zinc-800 pb-2 mb-3">
    {icon}
    <span>{children}</span>
  </div>
);

const SettingsModal = ({ open, onClose, onSaved }) => {
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [data, setData] = useState(null);
  const [activeTheme, setActiveTheme] = useState("auto");
  const [customs, setCustoms] = useState([]);
  const [bannedInput, setBannedInput] = useState("");
  const [banned, setBanned] = useState([]);
  const [queueSize, setQueueSize] = useState(5);
  const [newCustomName, setNewCustomName] = useState("");
  const [newCustomPrompt, setNewCustomPrompt] = useState("");

  useEffect(() => {
    if (!open) return;
    setLoading(true);
    getSettings()
      .then((s) => {
        setData(s);
        setActiveTheme(s.active_theme || "auto");
        setCustoms(s.custom_themes || []);
        setBanned(s.banned_words || []);
        setQueueSize(s.queue_size ?? 5);
      })
      .catch((e) => toast.error("Failed to load settings", { description: e.message }))
      .finally(() => setLoading(false));
  }, [open]);

  if (!open) return null;

  const addBanned = () => {
    const v = bannedInput.trim();
    if (!v) return;
    if (banned.some((b) => b.toLowerCase() === v.toLowerCase())) return;
    setBanned([...banned, v]);
    setBannedInput("");
  };

  const removeBanned = (w) => setBanned(banned.filter((b) => b !== w));

  const addCustom = () => {
    const name = newCustomName.trim();
    const prompt = newCustomPrompt.trim();
    if (!name || !prompt) {
      toast.error("Custom theme needs a name AND a prompt");
      return;
    }
    if (customs.some((c) => c.name.toLowerCase() === name.toLowerCase())) {
      toast.error("Theme name already exists");
      return;
    }
    setCustoms([...customs, { name, prompt }]);
    setNewCustomName("");
    setNewCustomPrompt("");
  };

  const removeCustom = (name) => {
    setCustoms(customs.filter((c) => c.name !== name));
    if (activeTheme === name || activeTheme === `custom:${name}`) {
      setActiveTheme("auto");
    }
  };

  const save = async () => {
    setSaving(true);
    try {
      await updateSettings({
        active_theme: activeTheme,
        custom_themes: customs,
        banned_words: banned,
        queue_size: queueSize,
      });
      toast.success("Settings saved // queue flushed");
      onSaved?.();
      onClose();
    } catch (e) {
      toast.error("Save failed", { description: e?.response?.data?.detail || e.message });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div
      data-testid="settings-modal"
      className="fixed inset-0 z-50 flex items-start justify-center p-6 bg-black/80 backdrop-blur-sm overflow-y-auto"
      onClick={onClose}
    >
      <div
        className="bg-zinc-950 border border-zinc-800 max-w-3xl w-full my-12 relative"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-6 py-5 border-b border-zinc-800">
          <h2 className="font-heading text-3xl uppercase tracking-tight">
            // Generator Settings
          </h2>
          <button
            data-testid="close-settings"
            onClick={onClose}
            className="p-2 hover:bg-zinc-900 transition-colors"
            aria-label="close"
          >
            <X size={20} weight="bold" />
          </button>
        </div>

        {loading || !data ? (
          <div className="p-10 text-xs text-zinc-500 uppercase tracking-widest">
            // loading config<span className="blink">_</span>
          </div>
        ) : (
          <div className="p-6 space-y-8">
            {/* Theme Selector */}
            <section data-testid="theme-section">
              <SectionTitle icon={<Stack size={14} weight="bold" />}>
                Active Theme Seed
              </SectionTitle>
              <p className="text-[11px] text-zinc-500 mb-3">
                Steers every new capsule. Saving will flush the pre-warmed queue.
              </p>
              <select
                data-testid="active-theme-select"
                value={activeTheme}
                onChange={(e) => setActiveTheme(e.target.value)}
                className="w-full bg-zinc-900 border border-zinc-700 px-3 py-2 text-sm text-zinc-200 font-body focus:outline-none focus:border-white"
              >
                <option value="auto">— AUTO / RANDOM —</option>
                <optgroup label="Built-in">
                  {(data.built_in_themes || []).map((t) => (
                    <option key={t.key} value={t.key}>
                      {t.name}
                    </option>
                  ))}
                </optgroup>
                {customs.length > 0 && (
                  <optgroup label="Custom">
                    {customs.map((c) => (
                      <option key={c.name} value={`custom:${c.name}`}>
                        {c.name}
                      </option>
                    ))}
                  </optgroup>
                )}
              </select>
            </section>

            {/* Custom Themes */}
            <section data-testid="custom-themes-section">
              <SectionTitle icon={<Plus size={14} weight="bold" />}>
                Custom Themes ({customs.length})
              </SectionTitle>
              <div className="space-y-2 mb-3">
                {customs.length === 0 && (
                  <p className="text-[11px] text-zinc-600 italic">// none yet</p>
                )}
                {customs.map((c) => (
                  <div
                    key={c.name}
                    data-testid={`custom-theme-${c.name}`}
                    className="flex items-start gap-3 bg-zinc-900 border border-zinc-800 p-3"
                  >
                    <div className="flex-1 min-w-0">
                      <p className="font-heading text-sm uppercase tracking-tight text-white">
                        {c.name}
                      </p>
                      <p className="text-[11px] text-zinc-400 mt-1 break-words">{c.prompt}</p>
                    </div>
                    <button
                      onClick={() => removeCustom(c.name)}
                      className="text-zinc-500 hover:text-red-400 transition-colors p-1"
                      aria-label={`remove ${c.name}`}
                    >
                      <X size={14} weight="bold" />
                    </button>
                  </div>
                ))}
              </div>
              <div className="grid grid-cols-1 md:grid-cols-3 gap-2">
                <input
                  data-testid="new-custom-name"
                  type="text"
                  placeholder="Theme name (e.g. Hospital Ghost)"
                  value={newCustomName}
                  onChange={(e) => setNewCustomName(e.target.value)}
                  className="bg-zinc-900 border border-zinc-700 px-3 py-2 text-sm text-zinc-200 font-body focus:outline-none focus:border-white"
                />
                <input
                  data-testid="new-custom-prompt"
                  type="text"
                  placeholder="Seed prompt fragment"
                  value={newCustomPrompt}
                  onChange={(e) => setNewCustomPrompt(e.target.value)}
                  className="md:col-span-1 bg-zinc-900 border border-zinc-700 px-3 py-2 text-sm text-zinc-200 font-body focus:outline-none focus:border-white"
                />
                <button
                  data-testid="add-custom-theme"
                  onClick={addCustom}
                  className="bg-zinc-100 text-black font-heading uppercase tracking-[0.2em] text-xs px-4 py-2 hover:bg-white transition-colors"
                >
                  + Add Theme
                </button>
              </div>
            </section>

            {/* Banned Words */}
            <section data-testid="banned-words-section">
              <SectionTitle icon={<Tag size={14} weight="bold" />}>
                Banned Words ({banned.length})
              </SectionTitle>
              <p className="text-[11px] text-zinc-500 mb-3">
                The AI will avoid these words. Useful for retiring tired concepts.
              </p>
              <div className="flex flex-wrap gap-2 mb-3">
                {banned.length === 0 && (
                  <p className="text-[11px] text-zinc-600 italic">// none yet</p>
                )}
                {banned.map((w) => (
                  <span
                    key={w}
                    data-testid={`banned-word-${w}`}
                    className="px-2 py-1 bg-zinc-900 border border-red-900/40 text-[11px] text-zinc-300 font-body uppercase tracking-wider flex items-center gap-2"
                  >
                    {w}
                    <button
                      onClick={() => removeBanned(w)}
                      className="text-zinc-500 hover:text-red-400 transition-colors"
                      aria-label={`remove ${w}`}
                    >
                      <X size={10} weight="bold" />
                    </button>
                  </span>
                ))}
              </div>
              <div className="flex gap-2">
                <input
                  data-testid="banned-word-input"
                  type="text"
                  placeholder="Add a word (e.g. skull)"
                  value={bannedInput}
                  onChange={(e) => setBannedInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      e.preventDefault();
                      addBanned();
                    }
                  }}
                  className="flex-1 bg-zinc-900 border border-zinc-700 px-3 py-2 text-sm text-zinc-200 font-body focus:outline-none focus:border-white"
                />
                <button
                  data-testid="add-banned-word"
                  onClick={addBanned}
                  className="bg-zinc-100 text-black font-heading uppercase tracking-[0.2em] text-xs px-4 py-2 hover:bg-white transition-colors"
                >
                  + Ban
                </button>
              </div>
            </section>

            {/* Queue size */}
            <section data-testid="queue-size-section">
              <SectionTitle>Pre-warm Queue Size: {queueSize}</SectionTitle>
              <input
                data-testid="queue-size-slider"
                type="range"
                min={0}
                max={15}
                value={queueSize}
                onChange={(e) => setQueueSize(parseInt(e.target.value, 10))}
                className="w-full accent-white"
              />
              <p className="text-[10px] text-zinc-500 mt-2">
                Higher = approve loads instantly but burns LLM credits faster.
              </p>
            </section>
          </div>
        )}

        <div className="border-t border-zinc-800 px-6 py-4 flex justify-end gap-3 sticky bottom-0 bg-zinc-950">
          <button
            onClick={onClose}
            className="px-4 py-2 text-xs uppercase tracking-[0.2em] font-body text-zinc-400 hover:text-white border border-zinc-800 hover:border-zinc-600"
          >
            Cancel
          </button>
          <button
            data-testid="save-settings"
            onClick={save}
            disabled={saving}
            className="px-5 py-2 bg-white text-black text-xs uppercase tracking-[0.2em] font-heading hover:bg-zinc-200 disabled:opacity-50 flex items-center gap-2"
          >
            <FloppyDisk size={14} weight="bold" />
            {saving ? "Saving..." : "Save"}
          </button>
        </div>
      </div>
    </div>
  );
};

export default SettingsModal;
