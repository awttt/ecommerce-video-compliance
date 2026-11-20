import { Navigate, Route, Routes } from "react-router-dom";
import { accessToken } from "./api";
import { AdminLayout } from "./layout";
import { AcceptInvite } from "./pages/Invite";
import { Login } from "./pages/Login";
import { Dashboard } from "./pages/Dashboard";
import { Flow } from "./pages/Flow";
import { Tenants } from "./pages/Tenants";
import { Members } from "./pages/Members";
import { Quota } from "./pages/Quota";
import { Keywords } from "./pages/Keywords";
import { Usage } from "./pages/Usage";
import { Audit } from "./pages/Audit";
import { Settings } from "./pages/Settings";

function RequireAuth() {
  if (!accessToken()) return <Navigate to="/login" replace />;
  return <AdminLayout />;
}

export function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/invite/:token" element={<AcceptInvite />} />
      <Route element={<RequireAuth />}>
        <Route path="/" element={<Dashboard />} />
        <Route path="/flow" element={<Flow />} />
        <Route path="/tenants" element={<Tenants />} />
        <Route path="/members" element={<Members />} />
        <Route path="/quota" element={<Quota />} />
        <Route path="/keywords" element={<Keywords />} />
        <Route path="/usage" element={<Usage />} />
        <Route path="/audit" element={<Audit />} />
        <Route path="/settings" element={<Settings />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
