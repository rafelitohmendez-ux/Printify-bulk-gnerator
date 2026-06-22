import "@/index.css";
import { BrowserRouter, Routes, Route, Link, useLocation } from "react-router-dom";
import { Toaster } from "sonner";
import Dashboard from "@/pages/Dashboard";
import History from "@/pages/History";

const NavBar = () => {
  const { pathname } = useLocation();
  const linkBase =
    "px-4 py-2 text-xs uppercase tracking-[0.25em] font-body border border-zinc-800 transition-colors";
  const active = "bg-zinc-100 text-black";
  const inactive = "text-zinc-400 hover:text-white hover:border-zinc-600";
  return (
    <header
      data-testid="top-nav"
      className="flex items-center justify-between px-6 py-4 border-b border-zinc-800 bg-zinc-950 relative z-10"
    >
      <Link to="/" className="flex items-center gap-3" data-testid="brand-logo">
        <div className="w-10 h-10 bg-white text-black flex items-center justify-center font-heading text-xl">
          MR
        </div>
        <div className="flex flex-col leading-none">
          <span className="font-heading text-2xl uppercase tracking-tight">
            MidnightRotation
          </span>
          <span className="text-[10px] text-zinc-500 uppercase tracking-[0.3em]">
            // Print-on-Demand // Approval Terminal
          </span>
        </div>
      </Link>
      <nav className="flex gap-2">
        <Link
          data-testid="nav-dashboard"
          to="/"
          className={`${linkBase} ${pathname === "/" ? active : inactive}`}
        >
          Generator
        </Link>
        <Link
          data-testid="nav-history"
          to="/history"
          className={`${linkBase} ${pathname === "/history" ? active : inactive}`}
        >
          Approved Archive
        </Link>
      </nav>
    </header>
  );
};

function App() {
  return (
    <BrowserRouter>
      <div className="min-h-screen bg-zinc-950 text-zinc-100 font-body">
        <NavBar />
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/history" element={<History />} />
        </Routes>
        <Toaster
          theme="dark"
          position="top-right"
          toastOptions={{
            style: {
              background: "#18181b",
              border: "1px solid #3f3f46",
              borderRadius: 0,
              fontFamily: "IBM Plex Mono, monospace",
              color: "#f4f4f5",
              fontSize: "12px",
              letterSpacing: "0.05em",
            },
          }}
        />
      </div>
    </BrowserRouter>
  );
}

export default App;
