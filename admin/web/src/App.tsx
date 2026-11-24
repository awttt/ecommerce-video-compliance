import { Navigate, Route, Routes } from "react-router-dom";
import { accessToken } from "./api";
import { AdminLayout } from "./layout";
import { ToolsFrame } from "./ToolsFrame";
import { AcceptInvite } from "./pages/Invite";
import { Login } from "./pages/Login";
import { Dashboard } from "./pages/Dashboard";
import { Tenants } from "./pages/Tenants";
import { Members } from "./pages/Members";
import { Quota } from "./pages/Quota";
import { SharedRules } from "./pages/SharedRules";
import { Usage } from "./pages/Usage";
import { Audit } from "./pages/Audit";
import { Settings } from "./pages/Settings";
import { Account } from "./pages/Account";
import { Recharge } from "./pages/Recharge";
import { ImageWorkbench } from "./pages/ImageWorkbench";

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
        <Route path="/tenants" element={<Tenants />} />
        <Route path="/members" element={<Members />} />
        <Route path="/quota" element={<Quota />} />
        <Route path="/shared-rules" element={<SharedRules />} />
        <Route path="/usage" element={<Usage />} />
        <Route path="/recharge" element={<Recharge />} />
        <Route path="/audit" element={<Audit />} />
        <Route path="/settings" element={<Settings />} />
        <Route path="/account" element={<Account />} />
        <Route path="/tools" element={<ToolsFrame />} />
        <Route path="/image-workbench" element={<ImageWorkbench />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
