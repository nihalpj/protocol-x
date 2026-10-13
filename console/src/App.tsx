import { Routes, Route } from "react-router";
import { Toaster } from "@/components/ui/sonner";
import ConsolePage from "./pages/ConsolePage";

export default function App() {
  return (
    <>
      <Routes>
        <Route path="/" element={<ConsolePage />} />
        <Route path="*" element={<ConsolePage />} />
      </Routes>
      <Toaster richColors position="bottom-right" />
    </>
  );
}
