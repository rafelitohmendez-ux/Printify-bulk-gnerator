// v2
import axios from "axios";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
const ADMIN_KEY = process.env.REACT_APP_ADMIN_API_KEY || "";
export const API_BASE = `${BACKEND_URL}/api`;

export const api = axios.create({
  baseURL: API_BASE,
  timeout: 180000,
  headers: {
    "X-Admin-Key": ADMIN_KEY,
  },
});

// Capsules
export const nextCapsule = () => api.get("/capsules/next").then((r) => r.data);
export const generateCapsule = () => api.post("/capsules/generate").then((r) => r.data);
export const approveCapsule = (id, payload) =>
  api.post(`/capsules/${id}/approve`, payload || {}).then((r) => r.data);
export const denyCapsule = (id) => api.post(`/capsules/${id}/deny`).then((r) => r.data);
export const regenerateImage = (id, side) =>
  api.post(`/capsules/${id}/regenerate-image/${side}`).then((r) => r.data);
export const listApproved = () => api.get("/capsules/approved").then((r) => r.data);
export const fetchStats = () => api.get("/capsules/stats").then((r) => r.data);
export const queueStatus = () => api.get("/capsules/queue/status").then((r) => r.data);
// The export needs the admin key header, so download it as a blob instead of a plain link
export const downloadCsv = async () => {
  const r = await api.get("/capsules/export.csv", { responseType: "blob" });
  const url = URL.createObjectURL(r.data);
  const a = document.createElement("a");
  a.href = url;
  a.download = "midnightrotation_approved.csv";
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
};
export const imageUrl = (id, side, bust) =>
  `${API_BASE}/capsules/${id}/image/${side}${bust ? `?t=${bust}` : ""}`;
export const pushToPrintify = (id) =>
  api.post(`/capsules/${id}/push-printify`).then((r) => r.data);

// Catalog fix: rewrite SEO + AI disclosure on live listings, fix photo order
export const startCatalogFix = (limit) =>
  api.post("/catalog/fix", { limit: limit || null }).then((r) => r.data);
export const catalogFixStatus = () => api.get("/catalog/fix").then((r) => r.data);

// Settings
export const getSettings = () => api.get("/settings").then((r) => r.data);
export const updateSettings = (payload) => api.put("/settings", payload).then((r) => r.data);
