import axios from "axios";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
export const API_BASE = `${BACKEND_URL}/api`;

export const api = axios.create({
  baseURL: API_BASE,
  timeout: 180000,
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
export const exportCsvUrl = () => `${API_BASE}/capsules/export.csv`;
export const imageUrl = (id, side, bust) =>
  `${API_BASE}/capsules/${id}/image/${side}${bust ? `?t=${bust}` : ""}`;

// Settings
export const getSettings = () => api.get("/settings").then((r) => r.data);
export const updateSettings = (payload) => api.put("/settings", payload).then((r) => r.data);
