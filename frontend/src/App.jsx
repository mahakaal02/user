import React, { useState } from "react";
import { useLiveSocket } from "./useLiveSocket.js";
import GlobalControls from "./components/GlobalControls.jsx";
import BotTable from "./components/BotTable.jsx";
import MarketChart from "./components/MarketChart.jsx";
import AnalyticsPanel from "./components/AnalyticsPanel.jsx";
import EventFeed from "./components/EventFeed.jsx";
import BotModal from "./components/BotModal.jsx";

export default function App() {
  useLiveSocket(); // one SSE connection feeding the store
  const [openBot, setOpenBot] = useState(null);
  return (
    <>
      <GlobalControls />
      <div className="grid">
        <div className="col">
          <BotTable onOpen={setOpenBot} />
        </div>
        <div className="col">
          <MarketChart />
          <AnalyticsPanel />
        </div>
        <div className="col">
          <EventFeed />
        </div>
      </div>
      {openBot && <BotModal id={openBot} onClose={() => setOpenBot(null)} />}
    </>
  );
}
