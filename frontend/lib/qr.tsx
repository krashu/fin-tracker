/**
 * Zero-dependency, offline QR Code Generator and SVG Renderer (PRD §F3b, ADR-0017).
 *
 * Implements ISO/IEC 18004 QR Code Byte Mode with Reed-Solomon Error Correction.
 * Renders directly as an SVG element without canvas, native binaries, or external API calls.
 */
import React, { useMemo } from "react";

// GF(256) tables with primitive polynomial 0x11d (285)
const EXP_TABLE = new Uint8Array(512);
const LOG_TABLE = new Uint8Array(256);

(() => {
  let val = 1;
  for (let i = 0; i < 255; i++) {
    EXP_TABLE[i] = val;
    EXP_TABLE[i + 255] = val;
    LOG_TABLE[val] = i;
    val = (val << 1) ^ (val >= 128 ? 0x11d : 0);
  }
})();

function gfMul(x: number, y: number): number {
  if (x === 0 || y === 0) return 0;
  return EXP_TABLE[LOG_TABLE[x] + LOG_TABLE[y]];
}

function rsGeneratorPoly(numEc: number): Uint8Array {
  let poly = new Uint8Array([1]);
  for (let i = 0; i < numEc; i++) {
    const next = new Uint8Array(poly.length + 1);
    const root = EXP_TABLE[i];
    for (let j = 0; j < poly.length; j++) {
      next[j] ^= gfMul(poly[j], root);
      next[j + 1] ^= poly[j];
    }
    poly = next;
  }
  return poly;
}

function rsComputeRemainder(data: Uint8Array, numEc: number): Uint8Array {
  const gen = rsGeneratorPoly(numEc);
  const remainder = new Uint8Array(numEc);
  for (let i = 0; i < data.length; i++) {
    const factor = data[i] ^ remainder[0];
    for (let j = 0; j < numEc - 1; j++) {
      remainder[j] = remainder[j + 1] ^ gfMul(gen[numEc - 1 - j], factor);
    }
    remainder[numEc - 1] = gfMul(gen[0], factor);
  }
  return remainder;
}

interface VersionSpec {
  version: number;
  totalDataCodewords: number;
  numEcCodewords: number;
  blocks: { numBlocks: number; dataPerBlock: number }[];
  alignmentCoords: number[];
}

// Version table for Byte Mode, Error Correction Level L
const VERSION_SPECS_L: VersionSpec[] = [
  {
    version: 1,
    totalDataCodewords: 19,
    numEcCodewords: 7,
    blocks: [{ numBlocks: 1, dataPerBlock: 19 }],
    alignmentCoords: [],
  },
  {
    version: 2,
    totalDataCodewords: 34,
    numEcCodewords: 10,
    blocks: [{ numBlocks: 1, dataPerBlock: 34 }],
    alignmentCoords: [6, 18],
  },
  {
    version: 3,
    totalDataCodewords: 55,
    numEcCodewords: 15,
    blocks: [{ numBlocks: 1, dataPerBlock: 55 }],
    alignmentCoords: [6, 22],
  },
  {
    version: 4,
    totalDataCodewords: 80,
    numEcCodewords: 20,
    blocks: [{ numBlocks: 1, dataPerBlock: 80 }],
    alignmentCoords: [6, 26],
  },
  {
    version: 5,
    totalDataCodewords: 108,
    numEcCodewords: 26,
    blocks: [{ numBlocks: 1, dataPerBlock: 108 }],
    alignmentCoords: [6, 30],
  },
  {
    version: 6,
    totalDataCodewords: 136,
    numEcCodewords: 18,
    blocks: [{ numBlocks: 2, dataPerBlock: 68 }],
    alignmentCoords: [6, 34],
  },
  {
    version: 7,
    totalDataCodewords: 156,
    numEcCodewords: 20,
    blocks: [{ numBlocks: 2, dataPerBlock: 78 }],
    alignmentCoords: [6, 22, 38],
  },
  {
    version: 8,
    totalDataCodewords: 194,
    numEcCodewords: 24,
    blocks: [{ numBlocks: 2, dataPerBlock: 97 }],
    alignmentCoords: [6, 24, 42],
  },
  {
    version: 9,
    totalDataCodewords: 232,
    numEcCodewords: 30,
    blocks: [{ numBlocks: 2, dataPerBlock: 116 }],
    alignmentCoords: [6, 26, 46],
  },
  {
    version: 10,
    totalDataCodewords: 274,
    numEcCodewords: 18,
    blocks: [
      { numBlocks: 2, dataPerBlock: 68 },
      { numBlocks: 2, dataPerBlock: 69 },
    ],
    alignmentCoords: [6, 28, 50],
  },
];

const FORMAT_BITS_L = [
  0x77c4, 0x72f3, 0x7daa, 0x789d, 0x662f, 0x6318, 0x6c41, 0x6976,
];

function selectVersion(dataLength: number): VersionSpec {
  for (const spec of VERSION_SPECS_L) {
    // 4 bits mode + (8 or 16 bits count) + data bytes
    const countBits = spec.version < 10 ? 8 : 16;
    const requiredBits = 4 + countBits + dataLength * 8;
    if (Math.ceil(requiredBits / 8) <= spec.totalDataCodewords) {
      return spec;
    }
  }
  throw new Error("Data too large for QR Code generator (max version 10)");
}

export function generateQrMatrix(text: string): boolean[][] {
  const encoder = new TextEncoder();
  const textBytes = encoder.encode(text);
  const spec = selectVersion(textBytes.length);

  // 1. Bit buffer
  const bits: number[] = [];
  const pushBits = (val: number, len: number) => {
    for (let i = len - 1; i >= 0; i--) {
      bits.push((val >> i) & 1);
    }
  };

  // Mode: Byte (0100)
  pushBits(0b0100, 4);
  // Character count indicator
  pushBits(textBytes.length, spec.version < 10 ? 8 : 16);
  // Data bytes
  for (let i = 0; i < textBytes.length; i++) {
    pushBits(textBytes[i], 8);
  }

  // Terminator (up to 4 bits of 0)
  const maxBits = spec.totalDataCodewords * 8;
  const termBits = Math.min(4, maxBits - bits.length);
  pushBits(0, termBits);

  // Pad to multiple of 8 bits
  while (bits.length % 8 !== 0) {
    bits.push(0);
  }

  // Pad bytes: alternate 0xEC and 0x11
  const padPatterns = [0xec, 0x11];
  let padIdx = 0;
  while (bits.length < maxBits) {
    pushBits(padPatterns[padIdx % 2], 8);
    padIdx++;
  }

  // Convert bits to byte codewords
  const dataCodewords = new Uint8Array(spec.totalDataCodewords);
  for (let i = 0; i < spec.totalDataCodewords; i++) {
    let byteVal = 0;
    for (let b = 0; b < 8; b++) {
      byteVal = (byteVal << 1) | bits[i * 8 + b];
    }
    dataCodewords[i] = byteVal;
  }

  // 2. Error correction codewords computation per block
  const blockData: Uint8Array[] = [];
  const blockEc: Uint8Array[] = [];
  let offset = 0;
  for (const blockGroup of spec.blocks) {
    for (let b = 0; b < blockGroup.numBlocks; b++) {
      const bData = dataCodewords.slice(offset, offset + blockGroup.dataPerBlock);
      offset += blockGroup.dataPerBlock;
      blockData.push(bData);
      blockEc.push(rsComputeRemainder(bData, spec.numEcCodewords));
    }
  }

  // 3. Interleaving data codewords, then EC codewords
  const interleaved: number[] = [];
  const maxDataPerBlock = Math.max(...blockData.map((b) => b.length));
  for (let i = 0; i < maxDataPerBlock; i++) {
    for (const bData of blockData) {
      if (i < bData.length) {
        interleaved.push(bData[i]);
      }
    }
  }
  for (let i = 0; i < spec.numEcCodewords; i++) {
    for (const bEc of blockEc) {
      interleaved.push(bEc[i]);
    }
  }

  // 4. Matrix size N x N
  const size = 17 + 4 * spec.version;
  const matrix: boolean[][] = Array.from({ length: size }, () =>
    Array(size).fill(false),
  );
  const isFunction: boolean[][] = Array.from({ length: size }, () =>
    Array(size).fill(false),
  );

  const setFunc = (r: number, c: number, val: boolean) => {
    if (r >= 0 && r < size && c >= 0 && c < size) {
      matrix[r][c] = val;
      isFunction[r][c] = true;
    }
  };

  // Finder patterns + Separators (7x7 + 1 border)
  const drawFinder = (top: number, left: number) => {
    for (let r = -1; r <= 7; r++) {
      for (let c = -1; c <= 7; c++) {
        const row = top + r;
        const col = left + c;
        if (row >= 0 && row < size && col >= 0 && col < size) {
          if (r >= 0 && r <= 6 && c >= 0 && c <= 6) {
            const isBorder = r === 0 || r === 6 || c === 0 || c === 6;
            const isCenter = r >= 2 && r <= 4 && c >= 2 && c <= 4;
            setFunc(row, col, isBorder || isCenter);
          } else {
            setFunc(row, col, false);
          }
        }
      }
    }
  };

  drawFinder(0, 0);
  drawFinder(0, size - 7);
  drawFinder(size - 7, 0);

  // Timing patterns
  for (let i = 8; i < size - 8; i++) {
    setFunc(6, i, i % 2 === 0);
    setFunc(i, 6, i % 2 === 0);
  }

  // Alignment patterns
  const coords = spec.alignmentCoords;
  for (let i = 0; i < coords.length; i++) {
    for (let j = 0; j < coords.length; j++) {
      const r = coords[i];
      const c = coords[j];
      // Skip if overlapping finder patterns
      if (
        (r <= 8 && c <= 8) ||
        (r <= 8 && c >= size - 9) ||
        (r >= size - 9 && c <= 8)
      ) {
        continue;
      }
      for (let dr = -2; dr <= 2; dr++) {
        for (let dc = -2; dc <= 2; dc++) {
          const isEdge = Math.abs(dr) === 2 || Math.abs(dc) === 2;
          const isCore = dr === 0 && dc === 0;
          setFunc(r + dr, c + dc, isEdge || isCore);
        }
      }
    }
  }

  // Dark module
  setFunc(4 * spec.version + 9, 8, true);

  // Reserve format information areas
  for (let i = 0; i <= 8; i++) {
    if (!isFunction[8][i]) isFunction[8][i] = true;
    if (!isFunction[i][8]) isFunction[i][8] = true;
  }
  for (let i = 0; i <= 7; i++) {
    if (!isFunction[size - 1 - i][8]) isFunction[size - 1 - i][8] = true;
    if (!isFunction[8][size - 1 - i]) isFunction[8][size - 1 - i] = true;
  }

  // 5. Data placement & masking (Mask 0: (r + c) % 2 === 0)
  const mask = 0;
  const isMasked = (r: number, c: number) => (r + c) % 2 === 0;

  // Flatten interleaved codewords to bit array
  const totalBits: number[] = [];
  for (const byteVal of interleaved) {
    for (let b = 7; b >= 0; b--) {
      totalBits.push((byteVal >> b) & 1);
    }
  }

  let bitIdx = 0;
  let upwards = true;
  for (let rightCol = size - 1; rightCol > 0; rightCol -= 2) {
    if (rightCol === 6) rightCol--; // Skip vertical timing column
    const cols = [rightCol, rightCol - 1];
    const rowRange = upwards
      ? Array.from({ length: size }, (_, idx) => size - 1 - idx)
      : Array.from({ length: size }, (_, idx) => idx);

    for (const r of rowRange) {
      for (const c of cols) {
        if (!isFunction[r][c]) {
          const bit = bitIdx < totalBits.length ? totalBits[bitIdx++] : 0;
          const val = (bit === 1) !== isMasked(r, c);
          matrix[r][c] = val;
        }
      }
    }
    upwards = !upwards;
  }

  // 6. Write format info bits (Level L, Mask 0)
  const formatVal = FORMAT_BITS_L[mask];
  const formatBits: boolean[] = [];
  for (let i = 14; i >= 0; i--) {
    formatBits.push(((formatVal >> i) & 1) === 1);
  }

  // Format bits around top-left finder
  const formatCoordsTopLeft = [
    [8, 0],
    [8, 1],
    [8, 2],
    [8, 3],
    [8, 4],
    [8, 5],
    [8, 7],
    [8, 8],
    [7, 8],
    [5, 8],
    [4, 8],
    [3, 8],
    [2, 8],
    [1, 8],
    [0, 8],
  ];
  for (let i = 0; i < 15; i++) {
    const [r, c] = formatCoordsTopLeft[i];
    matrix[r][c] = formatBits[i];
  }

  // Format bits split across bottom-left and top-right finders
  for (let i = 0; i < 7; i++) {
    matrix[size - 1 - i][8] = formatBits[i];
  }
  for (let i = 7; i < 15; i++) {
    matrix[8][size - 15 + i] = formatBits[i];
  }

  return matrix;
}

export interface QrCodeSvgProps extends React.SVGProps<SVGSVGElement> {
  value: string;
  size?: number;
  bgColor?: string;
  fgColor?: string;
  quietZone?: number;
  title?: string;
}

export function QrCodeSvg({
  value,
  size = 180,
  bgColor = "#ffffff",
  fgColor = "#000000",
  quietZone = 4,
  title = "UPI Payment QR Code",
  className,
  ...props
}: QrCodeSvgProps) {
  const matrix = useMemo(() => {
    try {
      return generateQrMatrix(value);
    } catch {
      return null;
    }
  }, [value]);

  if (!matrix) {
    return (
      <div
        className="flex items-center justify-center border border-border bg-muted/20 text-[11px] text-muted-foreground"
        style={{ width: size, height: size }}
      >
        Invalid QR Data
      </div>
    );
  }

  const moduleCount = matrix.length;
  const viewBoxSize = moduleCount + quietZone * 2;

  // Build path d string for all dark modules
  let path = "";
  for (let r = 0; r < moduleCount; r++) {
    for (let c = 0; c < moduleCount; c++) {
      if (matrix[r][c]) {
        path += `M${c + quietZone},${r + quietZone}h1v1h-1z `;
      }
    }
  }

  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox={`0 0 ${viewBoxSize} ${viewBoxSize}`}
      width={size}
      height={size}
      className={className}
      role="img"
      aria-label={title}
      {...props}
    >
      <title>{title}</title>
      <rect width={viewBoxSize} height={viewBoxSize} fill={bgColor} />
      <path d={path} fill={fgColor} shapeRendering="crispEdges" />
    </svg>
  );
}
