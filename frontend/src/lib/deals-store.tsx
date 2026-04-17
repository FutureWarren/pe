"use client";

import {
  createContext,
  ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";

import { deals as seedDeals } from "@/lib/mock-data";
import {
  buildDealFromHistoricalPayload,
  createDealFromBackendRun,
  fetchPilotRunPayload,
  refreshBackendDealState,
} from "@/lib/backend-pipeline";
import {
  applyMappingRowUpdate,
  applyReviewItemUpdate,
  buildSeedDeals,
  IntakeScanSummary,
  IntakeUploadInput,
  LOCAL_STORAGE_DEALS_KEY,
  mergeScanResultIntoDeal,
  regenerateDealOutput,
  refreshDealState,
  scanUploadedFiles,
} from "@/lib/local-pipeline";
import { enhanceScanResultWithGemini } from "@/lib/llm-definition";
import { Deal, ExceptionItem, MappingRow } from "@/lib/types";

interface CreateDealFromUploadsInput {
  dealName: string;
  sector: string;
  uploads: IntakeUploadInput[];
}

interface DealsStoreContextValue {
  deals: Deal[];
  hydrated: boolean;
  createDealFromUploads: (
    input: CreateDealFromUploadsInput,
  ) => Promise<{ dealId: string; scanSummary: IntakeScanSummary }>;
  scanIntoDeal: (
    dealId: string,
    uploads: IntakeUploadInput[],
  ) => Promise<IntakeScanSummary>;
  updateMappingRow: (
    dealId: string,
    rowId: string,
    updater: (row: MappingRow) => MappingRow,
  ) => void;
  updateReviewItemStatus: (
    dealId: string,
    reviewItemId: string,
    status: ExceptionItem["status"],
  ) => void;
  regenerateOutput: (dealId: string, outputId: string) => void;
  replaceDeals: (nextDeals: Deal[]) => void;
  importDealFromBackendRunId: (runId: string) => Promise<Deal>;
}

const DealsStoreContext = createContext<DealsStoreContextValue | null>(null);

function getDefaultDeals() {
  return buildSeedDeals(seedDeals);
}

function refreshPersistedDeal(deal: Deal) {
  return deal.processingEngine === "backend_python"
    ? refreshBackendDealState(deal)
    : refreshDealState(deal);
}

function applyBackendMappingRowUpdate(
  deal: Deal,
  rowId: string,
  updater: (row: MappingRow) => MappingRow,
) {
  const nextRows = deal.mappingRows.map((row) => (row.id === rowId ? updater(row) : row));
  const updatedRow = nextRows.find((row) => row.id === rowId);

  return refreshBackendDealState({
    ...deal,
    mappingRows: nextRows,
    definedItems: (deal.definedItems ?? []).map((item) =>
      item.id === `defined-${rowId}` && updatedRow
        ? {
            ...item,
            rawLabel: updatedRow.rawLineItemLabel,
            rawValue: updatedRow.rawValue,
            mappedCategory: updatedRow.mappedCategory,
            mappedMetric: updatedRow.mappedCategory,
            rationale: updatedRow.reasoning,
            reviewStatus: updatedRow.status,
            traceabilityStatus: updatedRow.sourceLinked ? "Traced" : "Missing",
            directOrDerived: updatedRow.directOrDerivedHint ?? item.directOrDerived,
            formulaDependencies:
              updatedRow.dependencyCandidatesHint ?? item.formulaDependencies,
            routingBucket: updatedRow.routingBucket,
            entersCorePipeline: updatedRow.entersCorePipeline,
            routingReason: updatedRow.routingReason,
          }
        : item,
    ),
  });
}

function applyBackendReviewItemStatus(
  deal: Deal,
  reviewItemId: string,
  status: ExceptionItem["status"],
) {
  return refreshBackendDealState({
    ...deal,
    exceptions: deal.exceptions.map((item) =>
      item.id === reviewItemId ? { ...item, status } : item,
    ),
  });
}

function regenerateBackendOutputLocally(deal: Deal, outputId: string) {
  return refreshBackendDealState({
    ...deal,
    outputs: deal.outputs.map((output) =>
      output.id === outputId
        ? { ...output, generatedDate: new Date().toISOString() }
        : output,
    ),
  });
}

export function DealsStoreProvider({ children }: { children: ReactNode }) {
  const [deals, setDeals] = useState<Deal[]>(getDefaultDeals);
  const [hydrated, setHydrated] = useState(false);

  useEffect(() => {
    try {
      const savedValue = window.localStorage.getItem(LOCAL_STORAGE_DEALS_KEY);

      if (!savedValue) {
        setHydrated(true);
        return;
      }

      const parsedDeals = JSON.parse(savedValue) as Deal[];
      setDeals(parsedDeals.map((deal) => refreshPersistedDeal(deal)));
    } catch {
      setDeals(getDefaultDeals());
    } finally {
      setHydrated(true);
    }
  }, []);

  useEffect(() => {
    if (!hydrated) {
      return;
    }

    window.localStorage.setItem(LOCAL_STORAGE_DEALS_KEY, JSON.stringify(deals));
  }, [deals, hydrated]);

  const createDealFromUploads = useCallback(
    async (input: CreateDealFromUploadsInput) => {
      const backendDeal = await createDealFromBackendRun(input);

      setDeals((currentDeals) => [backendDeal, ...currentDeals]);

      return {
        dealId: backendDeal.id,
        scanSummary: {
          fileCount: backendDeal.sourceFiles.length,
          financialTables: backendDeal.extractedItems.length,
          possibleIssues: backendDeal.exceptions.filter((item) => item.status === "Open").length,
          readinessScore: backendDeal.readinessScore,
        },
      };
    },
    [],
  );

  const scanIntoDeal = useCallback(async (dealId: string, uploads: IntakeUploadInput[]) => {
    const localScanResult = await scanUploadedFiles(uploads);
    const scanResult = await enhanceScanResultWithGemini(localScanResult);

    setDeals((currentDeals) =>
      currentDeals.map((deal) =>
        deal.id === dealId ? mergeScanResultIntoDeal(deal, scanResult) : deal,
      ),
    );

    return scanResult.scanSummary;
  }, []);

  const updateMappingRow = useCallback(
    (dealId: string, rowId: string, updater: (row: MappingRow) => MappingRow) => {
      setDeals((currentDeals) =>
        currentDeals.map((deal) =>
          deal.id === dealId
            ? deal.processingEngine === "backend_python"
              ? applyBackendMappingRowUpdate(deal, rowId, updater)
              : applyMappingRowUpdate(deal, rowId, updater)
            : deal,
        ),
      );
    },
    [],
  );

  const updateReviewItemStatus = useCallback(
    (dealId: string, reviewItemId: string, status: ExceptionItem["status"]) => {
      setDeals((currentDeals) =>
        currentDeals.map((deal) =>
          deal.id === dealId
            ? deal.processingEngine === "backend_python"
              ? applyBackendReviewItemStatus(deal, reviewItemId, status)
              : applyReviewItemUpdate(deal, reviewItemId, status)
            : deal,
        ),
      );
    },
    [],
  );

  const regenerateOutput = useCallback((dealId: string, outputId: string) => {
    setDeals((currentDeals) =>
      currentDeals.map((deal) =>
        deal.id === dealId
          ? deal.processingEngine === "backend_python"
            ? regenerateBackendOutputLocally(deal, outputId)
            : regenerateDealOutput(deal, outputId)
          : deal,
      ),
    );
  }, []);

  const importDealFromBackendRunId = useCallback(async (runId: string) => {
    const payload = await fetchPilotRunPayload(runId);
    const deal = buildDealFromHistoricalPayload(payload);

    setDeals((currentDeals) => {
      const index = currentDeals.findIndex((item) => item.backendRun?.runId === runId);
      if (index >= 0) {
        const next = [...currentDeals];
        next[index] = deal;
        return next;
      }
      return [deal, ...currentDeals];
    });

    return deal;
  }, []);

  const value = useMemo(
    () => ({
      deals,
      hydrated,
      createDealFromUploads,
      scanIntoDeal,
      updateMappingRow,
      updateReviewItemStatus,
      regenerateOutput,
      replaceDeals: setDeals,
      importDealFromBackendRunId,
    }),
    [
      createDealFromUploads,
      deals,
      hydrated,
      importDealFromBackendRunId,
      regenerateOutput,
      scanIntoDeal,
      updateMappingRow,
      updateReviewItemStatus,
    ],
  );

  return (
    <DealsStoreContext.Provider value={value}>{children}</DealsStoreContext.Provider>
  );
}

export function useDealsStore() {
  const context = useContext(DealsStoreContext);

  if (!context) {
    throw new Error("useDealsStore must be used within DealsStoreProvider.");
  }

  return context;
}

export function useDealById(dealId: string) {
  const { deals } = useDealsStore();

  return deals.find((deal) => deal.id === dealId);
}

export function useOutputById(dealId: string, outputId: string) {
  return useDealById(dealId)?.outputs.find((output) => output.id === outputId);
}
