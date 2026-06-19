import axios from "axios";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
export const API_BASE = `${BACKEND_URL}/api`;

export const api = axios.create({
  baseURL: API_BASE,
  timeout: 120000,
});

export const generateCapsule = () => api.post("/capsules/generate").then((r) => r.data);
export const approveCapsule = (id) => api.post(`/capsules/${id}/approve`).then((r) => r.data);
export const denyCapsule = (id) => api.post(`/capsules/${id}/deny`).then((r) => r.data);
export const listApproved = () => api.get("/capsules/approved").then((r) => r.data);
export const fetchStats = () => api.get("/capsules/stats").then((r) => r.data);
export const exportCsvUrl = () => `${API_BASE}/capsules/export.csv`;
export const imageUrl = (id, side) => `${API_BASE}/capsules/${id}/image/${side}`;
