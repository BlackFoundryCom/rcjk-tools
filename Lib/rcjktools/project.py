import logging
import math
import re
from typing import NamedTuple
import operator
import pathlib

from fontTools.misc.transform import Transform
from fontTools.pens.recordingPen import RecordingPointPen
from fontTools.pens.pointPen import SegmentToPointPen, PointToSegmentPen
from fontTools.ufoLib.glifLib import readGlyphFromString
from fontTools.ufoLib.filenames import userNameToFileName
from fontTools.varLib.models import VariationModel
from ufoLib2.objects import Font as UFont, Glyph as UGlyph


class InterpolationError(Exception):
    pass


class RoboCJKProject:

    def __init__(self, path):
        self._path = pathlib.Path(path)
        self.characterGlyphGlyphSet = GlyphSet(self._path / "characterGlyph")
        self.deepComponentGlyphSet = GlyphSet(self._path / "deepComponent")
        self.atomicElementGlyphSet = GlyphSet(self._path / "atomicElement")

    def getGlyphNamesAndUnicodes(self):
        return self.characterGlyphGlyphSet.getGlyphNamesAndUnicodes()

    def drawCharacterGlyph(self, glyphName, location, pen):
        outline, dcItems, width = self.instantiateCharacterGlyph(glyphName, location)
        outline.draw(pen)
        for dcName, atomicElements in dcItems:
            for aeName, atomicOutline in atomicElements:
                atomicOutline.draw(pen)
        return width

    def instantiateCharacterGlyph(self, glyphName, location):
        glyph = self.characterGlyphGlyphSet.getGlyph(glyphName)
        glyph = glyph.instantiate(location)
        deepItems = []
        for component in glyph.components:
            deepItem = self.instantiateDeepComponent(
                component.name, component.coord,
                makeTransform(**component.transform),
            )
            deepItems.append((component.name, deepItem))
        return glyph.outline, deepItems, glyph.width

    def instantiateDeepComponent(self, glyphName, location, transform):
        glyph = self.deepComponentGlyphSet.getGlyph(glyphName)
        glyph = glyph.instantiate(location)
        atomicOutlines = []
        for component in glyph.components:
            t = transform.transform(makeTransform(**component.transform))
            atomicOutline = self.instantiateAtomicElement(
                component.name, component.coord, t,
            )
            atomicOutlines.append((component.name, atomicOutline))
        return atomicOutlines

    def instantiateAtomicElement(self, glyphName, location, transform):
        glyph = self.atomicElementGlyphSet.getGlyph(glyphName)
        glyph = glyph.instantiate(location)
        return glyph.outline.transform(transform)

    def saveFlattenedUFO(self, ufoPath, location, familyName, styleName):
        ufo = setupFont(familyName, styleName)
        self.addFlattenedGlyphsToUFO(ufo, location)
        ufo.save(ufoPath, overwrite=True)

    def addFlattenedGlyphsToUFO(self, ufo, location):
        revCmap = self.getGlyphNamesAndUnicodes()
        glyphNames = filterGlyphNames(sorted(revCmap))
        for glyphName in glyphNames:
            glyph = UGlyph(glyphName)
            glyph.unicodes = revCmap[glyphName]
            pen = glyph.getPen()
            try:
                width = self.drawCharacterGlyph(glyphName, location, pen)
            except InterpolationError as e:
                print(f"glyph {glyphName} can't be interpolated ({e})")
            else:
                glyph.width = max(0, width)  # can't be negative
                ufo[glyphName] = glyph

    def saveVarCoUFO(self, ufoPath, familyName, styleName):
        ufo = setupFont(familyName, styleName)

        revCmap = self.getGlyphNamesAndUnicodes()
        glyphNames = filterGlyphNames(sorted(revCmap))
        glyphNames = glyphNames[:200]  # tmp subset
        for glyphName in glyphNames:
            addRCJKGlyphToVarCoUFO(ufo, self.characterGlyphGlyphSet, glyphName, revCmap[glyphName])
        ufo.save(ufoPath, overwrite=True)


def addRCJKGlyphToVarCoUFO(ufo, rcjkGlyphSet, glyphName, unicodes):
    rcjkGlyph = rcjkGlyphSet.getGlyph(glyphName)
    glyph = UGlyph(glyphName)
    glyph.unicodes = unicodes
    glyph.width = rcjkGlyph.width

    rcjkGlyphToVarCoGlyph(rcjkGlyph, glyph)

    packedAxes = packAxes(rcjkGlyph.axes)
    if packedAxes:
        glyph.lib["varco.axes"] = packedAxes
    ufo[glyphName] = glyph
    for rcjkVarGlyph in rcjkGlyph.variations:
        layerName = layerNameFromLocation(rcjkVarGlyph.location)
        layer = getUFOLayer(ufo, layerName)
        varGlyph = UGlyph(glyphName)
        varGlyph.width = rcjkVarGlyph.width
        rcjkGlyphToVarCoGlyph(rcjkVarGlyph, varGlyph)
        varGlyph.lib["varco.location"] = rcjkVarGlyph.location
        layer[glyphName] = varGlyph


def rcjkGlyphToVarCoGlyph(rcjkGlyph, glyph):
    pen = glyph.getPointPen()
    rcjkGlyph.drawPoints(pen)
    compoVarInfo = []
    for compo in rcjkGlyph.components:
        # (x, y, rotation, scalex, scaley, rcenterx, rcentery)
        transform = compo.transform
        t = (transform["scalex"], 0, 0, transform["scaley"], transform["x"], transform["y"])
        pen.addComponent(compo.name, t)
        varCoTransform = dict(rotation=transform["rotation"], rcenterx=transform["rcenterx"], rcentery=transform["rcentery"])
        compoVarInfo.append(dict(coord=compo.coord, transform=varCoTransform))
    if compoVarInfo:
        glyph.lib["varco.components"] = compoVarInfo


def packAxes(axes):
    return [dict(name=axisName, minValue=minValue, maxValue=maxValue)
            for axisName, (minValue, maxValue) in axes.items()]


def setupFont(familyName, styleName):
    ufo = UFont()
    ufo.info.familyName = familyName
    ufo.info.styleName = styleName
    ufo.info.unitsPerEm = 1000
    ufo.info.descender = -120
    ufo.info.ascender = ufo.info.unitsPerEm + ufo.info.descender
    return ufo


def filterGlyphNames(glyphNames):
    okGlyphNames = []
    for glyphName in glyphNames:
        try:
            glyphName.encode("ascii")
        except UnicodeEncodeError:
            print(f"WARNING glyph name {glyphName} is not ASCII, and will not be exported")
        else:
            okGlyphNames.append(glyphName)
    return okGlyphNames


def getUFOLayer(ufo, layerName):
    if layerName not in ufo.layers:
        layer = ufo.newLayer(layerName)
    else:
        layer = ufo.layers[layerName]
    return layer


def layerNameFromLocation(location):
    location = sorted(location.items())
    nameParts = []
    for name, value in location:
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        nameParts.append(f"{name}={value}")
    return "+".join(nameParts)


_glyphNamePat = re.compile(rb'<glyph\s+name\s*=\s*"([^"]+)"')
_unicodePat = re.compile(rb'<unicode\s+hex\s*=\s*"([^"]+)"')


class GlyphSet:

    def __init__(self, path):
        self._path = path
        self._glyphs = {}
        self._layers = {}
        self._revCmap = None

    def getGlyphNamesAndUnicodes(self):
        if self._revCmap is None:
            glyphNames = {}
            for path in self._path.glob("*.glif"):
                with open(path, "rb") as f:
                    # assuming all unicodes are in the first 1024 bytes of the file
                    data = f.read(1024)
                m = _glyphNamePat.search(data)
                if m is None:
                    raise ValueError(
                        f"invalid .glif file, glyph name not found ({path})"
                    )
                glyphName = m.group(1).decode("utf-8")
                refFileName = userNameToFileName(glyphName, suffix=".glif")
                if refFileName != path.name:
                    logging.warning(
                        f"actual file name does not match predicted file name: "
                        f"{refFileName} {path.name} {glyphName}"
                    )
                unicodes = [int(u, 16) for u in _unicodePat.findall(data)]
                glyphNames[glyphName] = unicodes
            self._revCmap = glyphNames
        return self._revCmap

    def __contains__(self, glyphName):
        if glyphName in self._glyphs:
            return True
        fileName = userNameToFileName(glyphName, suffix=".glif")
        glyphPath = self._path / fileName
        return glyphPath.exists()

    def getGlyph(self, glyphName):
        glyph = self._glyphs.get(glyphName)
        if glyph is None:
            fileName = userNameToFileName(glyphName, suffix=".glif")
            glyph = parseGlyph(self._path / fileName)
            glyph._postParse(self)
            self._glyphs[glyphName] = glyph
        return glyph

    def getLayer(self, layerName):
        layer = self._layers.get(layerName)
        if layer is None:
            layer = GlyphSet(self._path / layerName)
            self._layers[layerName] = layer
        return layer


class _MathMixin:

    def __add__(self, other):
        return self._doBinaryOperator(other, operator.add)

    def __sub__(self, other):
        return self._doBinaryOperator(other, operator.sub)

    def __mul__(self, scalar):
        return self._doUnaryOperator(scalar, operator.mul)

    def __rmul__(self, scalar):
        return self._doUnaryOperator(scalar, operator.mul)


class Glyph(_MathMixin):

    def __init__(self):
        self.name = None
        self.width = 0
        self.unicodes = []
        self.outline = MathOutline()
        self.components = []
        self.lib = {}
        self.location = {}  # neutral
        self.axes = {}
        self.variations = []
        self.model = None
        self.deltas = None

    def _postParse(self, glyphSet):
        """This gets called soon after parsing the .glif file. Any layer glyphs
        and variation info is unpacked here, and put into a subglyph, as part
        of the self.variations list.
        """
        dcNames = []
        for dc in self.lib.get("robocjk.deepComponents", []):
            dcNames.append(dc["name"])
            self.components.append(_unpackDeepComponent(dc))

        varKey = _getVarKey(self.lib)
        if varKey is None:
            return

        for axisName, varDict in self.lib[varKey].items():
            layerName = varDict["layerName"]
            minValue = varDict["minValue"]
            maxValue = varDict["maxValue"]
            if not self.outline.isEmpty():
                layer = glyphSet.getLayer(layerName)
                if self.name in layer:
                    varGlyph = layer.getGlyph(self.name)
                else:
                    # Layer glyph does not exist, make one up by copying
                    # self.width and self.outline
                    varGlyph = Glyph()
                    varGlyph.width = self.width
                    varGlyph.outline = self.outline
            else:
                varGlyph = Glyph()
                varGlyph.width = self.width

            varGlyph.location = {axisName: 1.0}
            self.axes[axisName] = (minValue, maxValue)

            deepComponents = varDict["content"]["deepComponents"]
            assert len(dcNames) == len(deepComponents)
            for dc, dcName in zip(deepComponents, dcNames):
                varGlyph.components.append(_unpackDeepComponent(dc, dcName))
            assert len(varGlyph.components) == len(self.components)

            self.variations.append(varGlyph)

        locations = [{}] + [variation.location for variation in self.variations]
        self.model = VariationModel(locations)

    def getPointPen(self):
        return self.outline

    def getPen(self):
        return SegmentToPointPen(self.outline)

    def drawPoints(self, pen):
        self.outline.drawPoints(pen)

    def draw(self, pen):
        self.outline.draw(pen)

    def instantiate(self, location):
        if self.model is None:
            return self  # XXX raise error?
        if self.deltas is None:
            self.deltas = self.model.getDeltas([self] + self.variations)
        location = normalizeLocation(location, self.axes)
        return self.model.interpolateFromDeltas(location, self.deltas)

    def _doUnaryOperator(self, scalar, op):
        result = Glyph()
        result.name = self.name
        result.unicodes = self.unicodes
        result.width = op(self.width, scalar)
        result.outline = op(self.outline, scalar)
        result.components = [op(compo, scalar) for compo in self.components]
        return result

    def _doBinaryOperator(self, other, op):
        result = Glyph()
        result.name = self.name
        result.unicodes = self.unicodes
        result.width = op(self.width, other.width)
        result.outline = op(self.outline, other.outline)
        result.components = [
            op(compo1, compo2)
            for compo1, compo2 in zip(self.components, other.components)
        ]
        return result


def normalizeValue(value, minValue, maxValue):
    assert minValue < maxValue
    return (value - minValue) / (maxValue - minValue)


def normalizeLocation(location, axes):
    location = {axisName: normalizeValue(v, *axes.get(axisName, (0, 1)))
                for axisName, v in location.items()}
    return _clampLocation(location)


def _clampLocation(d):
    return {k: min(1, max(0, v)) for k, v in d.items()}


class Component(NamedTuple):

    name: str
    coord: dict
    transform: dict

    def __add__(self, other):
        return Component(
            self.name,
            self.coord + other.coord,
            self.transform + other.transform,
        )

    def __sub__(self, other):
        return Component(
            self.name,
            self.coord - other.coord,
            self.transform - other.transform,
        )

    def __mul__(self, scalar):
        return Component(
            self.name,
            self.coord * scalar,
            self.transform * scalar,
        )

    def __rmul__(self, scalar):
        return self.__mul__(scalar)


class MathDict(dict, _MathMixin):

    def _doUnaryOperator(self, scalar, op):
        result = MathDict()
        for k, v in self.items():
            if isinstance(v, (int, float)):
                result[k] = op(v, scalar)
            else:
                result[k] = v
        return result

    def _doBinaryOperator(self, other, op):
        # any missing keys will be taken from the other dict
        self_other = dict(other)
        self_other.update(self)
        other_self = dict(self)
        other_self.update(other)
        result = MathDict()
        for k, v1 in self_other.items():
            v2 = other_self[k]
            if isinstance(v1, (int, float)):
                result[k] = op(v1, v2)
            else:
                if v1 != v2:
                    raise InterpolationError("incompatible dicts")
                result[k] = v1
        return result


class MathOutline(RecordingPointPen, _MathMixin):

    def isEmpty(self):
        return not self.value

    def drawPoints(self, pen):
        self.replay(pen)

    def draw(self, pen):
        self.drawPoints(PointToSegmentPen(pen))

    def transform(self, t):
        if not hasattr(t, "transformPoint"):
            t = Transform(*t)
        return self.applyUnaryFunc(t.transformPoint)

    def applyUnaryFunc(self, func):
        result = MathOutline()
        for m, args, kwargs in self.value:
            if m == "addPoint":
                pt, seg, smooth, name = args
                result.addPoint(func(pt), seg, smooth, name, **kwargs)
            elif m == "beginPath":
                result.beginPath()
            elif m == "endPath":
                result.endPath()
            else:
                assert False, f"unsupported method: {m}"
        return result

    def _doUnaryOperator(self, scalar, op):
        def func(pt):
            x, y = pt
            return op(x, scalar), op(y, scalar)
        return self.applyUnaryFunc(func)

    def _doBinaryOperator(self, other, op):
        result = MathOutline()
        if len(self.value) != len(other.value):
            raise InterpolationError("incompatible outline")

        for (m1, args1, kwargs1), (m2, args2, kwargs2) in zip(self.value, other.value):
            if m1 != m2:
                raise InterpolationError("incompatible outline")
            if m1 == "addPoint":
                (x1, y1), seg1, smooth1, name1 = args1
                (x2, y2), seg2, smooth2, name2 = args2
                if seg1 != seg2:
                    raise InterpolationError("incompatible outline")
                pt = op(x1, x2), op(y1, y2)
                result.addPoint(pt, seg1, smooth1, name1, **kwargs1)
            elif m1 == "beginPath":
                result.beginPath()
            elif m1 == "endPath":
                result.endPath()
            else:
                assert False, f"unsupported method: {m1}"
        return result


def parseGlyph(p):
    with open(p) as f:
        data = f.read()
    g = Glyph()
    readGlyphFromString(data, g, g.getPointPen())
    return g


def makeTransform(x, y, rotation, scalex, scaley, rcenterx, rcentery):
    rotation = math.radians(rotation)
    rcenterx *= scalex
    rcentery *= scaley
    t = Transform()
    t = t.translate(x + rcenterx, y + rcentery)
    t = t.rotate(rotation)
    t = t.translate(-rcenterx, -rcentery)
    t = t.scale(scalex, scaley)
    return t


_rcjkTransformParameters = set(makeTransform.__code__.co_varnames[:makeTransform.__code__.co_argcount])


def _unpackDeepComponent(dc, name=None):
    if name is None:
        # "name" is defined in neutral components, but is implied in variations
        name = dc["name"]
    coord = dc["coord"]
    transform = {k: v for k, v in dc.items() if k in _rcjkTransformParameters}
    return Component(name, MathDict(coord), MathDict(transform))


def _getVarKey(lib):
    roboVarKeys = (
        "robocjk.fontVariationGlyphs",
        "robocjk.glyphVariationGlyphs",
    )
    for varKey in roboVarKeys:
        if varKey in lib:
            return varKey
    return None


if __name__ == "__main__":
    # DrawBot test snippet
    from drawBot import BezierPath, translate, scale, fill, stroke, drawPath

    def drawOutline(outline):
        bez = BezierPath()
        outline.drawPoints(bez)
        drawPath(bez)

    testPath = "/Users/just/code/git/BlackFoundry/gs-cjk-rcjk/Hanzi.rcjk/"
    project = RoboCJKProject(testPath)

    glyphName = "uni3A00"
    # glyphName = "uni2EBB"
    # glyphName = "uni3A10"
    # glyphName = "uni3A69"  # outlines
    glyphName = "uni540E"  # mix outlines / components

    translate(100, 200)
    scale(0.8)
    fill(0, 0.02)
    stroke(0)
    steps = 3
    for i in range(steps):
        f = i / (steps - 1)
        outline, deepItems, width = project.instantiateCharacterGlyph(glyphName, {"wght": f})
        if outline is not None:
            drawOutline(outline)
        for dcName, atomicOutlines in deepItems:
            for atomicName, atomicOutline in atomicOutlines:
                drawOutline(atomicOutline)
