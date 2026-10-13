import { useState } from "react";
import { api, useApi } from "@/api";
import { HeaderBar } from "@/components/console/HeaderBar";
import { Registry } from "@/components/console/Registry";
import { Telemetry } from "@/components/console/Telemetry";
import { Governance } from "@/components/console/Governance";
import { SnippetsPage } from "@/components/console/SnippetsPage";
import { AddPackageModal } from "@/components/console/AddPackageModal";
import { SettingsModal } from "@/components/console/SettingsModal";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";

export default function ConsolePage() {
  const statusQ = useApi(api.status, 5000);
  const [addOpen, setAddOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);

  const agentEnabled = statusQ.data?.agent_features_enabled ?? false;

  return (
    <div className="flex h-screen flex-col overflow-hidden">
      <HeaderBar status={statusQ.data} error={statusQ.error} onOpenSettings={() => setSettingsOpen(true)} />
      <Tabs defaultValue="registry" className="flex min-h-0 flex-1 flex-col">
        <div className="border-b border-[#e4e4e7] bg-white px-4 pt-2">
          <TabsList>
            <TabsTrigger value="registry" className="text-[11px]">
              Registry
            </TabsTrigger>
            <TabsTrigger value="telemetry" className="text-[11px]">
              Telemetry
            </TabsTrigger>
            <TabsTrigger value="governance" className="text-[11px]">
              Governance
            </TabsTrigger>
            <TabsTrigger value="snippets" className="text-[11px]">
              Snippets
            </TabsTrigger>
          </TabsList>
        </div>
        <TabsContent value="registry" className="mt-0 min-h-0 flex-1 overflow-hidden">
          <Registry onOpenAdd={() => setAddOpen(true)} />
        </TabsContent>
        <TabsContent value="telemetry" className="mt-0 min-h-0 flex-1 overflow-hidden">
          <Telemetry />
        </TabsContent>
        <TabsContent value="governance" className="mt-0 min-h-0 flex-1 overflow-hidden">
          <Governance />
        </TabsContent>
        <TabsContent value="snippets" className="mt-0 min-h-0 flex-1 overflow-hidden">
          <SnippetsPage />
        </TabsContent>
      </Tabs>

      {addOpen && <AddPackageModal onClose={() => setAddOpen(false)} agentEnabled={agentEnabled} />}
      {settingsOpen && <SettingsModal onClose={() => setSettingsOpen(false)} />}
    </div>
  );
}
