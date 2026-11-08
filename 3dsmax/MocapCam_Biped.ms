/*
    MocapCam -> Biped  (3ds Max)
    ---------------------------------------------------------------------------
    Transfère une animation sur squelette Mixamo (FBX MocapCam, ou toute
    animation Mixamo) vers un Biped natif de 3ds Max (Character Studio).

    Utilisation :
      1. Importez le FBX (Fichier > Importer) : le squelette « mixamorig:... »
         apparaît dans la scène avec son animation.
      2. Faites glisser ce fichier dans une fenêtre 3ds Max
         (ou Script > Exécuter le script…).
      3. Choisissez un os du squelette Mixamo, puis une partie du Biped,
         et cliquez sur « Transférer l'animation ».

    Le Biped peut avoir sa propre morphologie (taille, proportions, T-pose ou
    A-pose en mode Figure, nombre de vertèbres et de phalanges) : chaque os du
    Biped prend la direction de l'os correspondant, et sa torsion est reprise
    de l'animation. Le déplacement est mis à l'échelle de ses jambes.

    Un projet MocapCam — https://github.com/BaptisteTTD/MocapCam
*/

(
-- ============================================================================ outils vectoriels
-- convention 3ds Max : v_monde = v_local * M, les lignes d'une matrix3 sont les axes locaux

global mcb_nrm, mcb_rot, mcb_tr, mcb_frame, mcb_clamp, mcb_smooth, mcb_perp, mcb_rotAbout
global mcb_swing, mcb_hinge, mcb_hingeDir, mcb_rotAngle, mcb_pct, mcb_median5, mcb_toQuat
global mcb_shortName, mcb_allNodes, mcb_chains, mcb_tgtOf, mcb_transfer, mcb_findCom
global MocapCamBipedRollout, mcb_noUI

fn mcb_nrm v = (
    local l = length v
    if l > 1e-9 then v / l else [0, 0, 0]
)

-- partie rotation d'une matrice (sans échelle)
fn mcb_rot m = matrix3 (mcb_nrm m.row1) (mcb_nrm m.row2) (mcb_nrm m.row3) [0, 0, 0]

-- transposée = inverse d'une rotation
fn mcb_tr m = matrix3 [m.row1.x, m.row2.x, m.row3.x] [m.row1.y, m.row2.y, m.row3.y] [m.row1.z, m.row2.z, m.row3.z] [0, 0, 0]

-- repère orthonormé : X = d, Z = r rendu perpendiculaire à d, Y = Z ^ X
fn mcb_frame d r = (
    local x = mcb_nrm d
    local z = mcb_nrm (r - x * (dot r x))
    matrix3 x (cross z x) z [0, 0, 0]
)

fn mcb_clamp v lo hi = (if v < lo then lo else (if v > hi then hi else v))

fn mcb_smooth a lo hi = (
    local t = mcb_clamp ((a - lo) / (hi - lo)) 0.0 1.0
    t * t * (3.0 - 2.0 * t)
)

fn mcb_perp v n = mcb_nrm (v - n * (dot v n))

-- rotation d'un vecteur autour d'un axe unitaire (règle de la main droite, degrés)
fn mcb_rotAbout v h deg = (
    local c = cos deg
    v * c + (cross h v) * (sin deg) + h * ((dot h v) * (1.0 - c))
)

-- plus petite rotation qui amène l'axe X de M0 sur d (clavicule : pas de torsion)
fn mcb_swing M0 d = (
    local x0 = mcb_nrm M0.row1
    local x1 = mcb_nrm d
    local ax = cross x0 x1
    local s = length ax
    if s < 1e-6 then M0 else (
        ax = ax / s
        local a = atan2 s (dot x0 x1)
        matrix3 (mcb_rotAbout M0.row1 ax a) (mcb_rotAbout M0.row2 ax a) (mcb_rotAbout M0.row3 ax a) [0, 0, 0]
    )
)

-- os à un seul axe (coude, genou, phalanges) : rotation autour de son axe Z local seulement,
-- flexion bornée dans le sens anatomique (h)
fn mcb_hinge M0 d h lo hi = (
    local x0 = M0.row1
    local y0 = M0.row2
    local z0 = M0.row3
    local sg = if (dot z0 h) >= 0 then 1.0 else -1.0
    local dp = d - z0 * (dot d z0)
    local th = atan2 (dot dp y0) (dot dp x0)
    th = sg * (mcb_clamp (sg * th) lo hi)
    local cs = cos th
    local sn = sin th
    matrix3 (x0 * cs + y0 * sn) (y0 * cs - x0 * sn) z0 [0, 0, 0]
)

-- axe de flexion d'une articulation à deux segments : exact quand elle est pliée, référence
-- (hpref) quand elle est presque tendue, avec un fondu entre les deux (pas de bascule)
fn mcb_hingeDir d1 d2 hpref = (
    local a = mcb_nrm d1
    local hp = mcb_perp hpref a
    local hc = cross a (mcb_nrm d2)
    local s = length hc
    if s < 1e-9 then hp else (
        hc = hc / s
        if (dot hc hp) < 0 do hc = [0, 0, 0] - hc
        local w = mcb_smooth (asin (mcb_clamp s 0.0 1.0)) 5.0 20.0
        mcb_nrm (hc * w + hp * (1.0 - w))
    )
)

-- angle (degrés) entre deux rotations
fn mcb_rotAngle A B = (
    local M = A * (mcb_tr B)
    acos (mcb_clamp ((M.row1.x + M.row2.y + M.row3.z - 1.0) / 2.0) (-1.0) 1.0)
)

fn mcb_pct arr p = (
    local a = sort (for v in arr collect v)
    if a.count == 0 then 0.0 else a[((floor ((a.count - 1) * p / 100.0 + 0.5)) as integer) + 1]
)

fn mcb_median5 arr = (
    local n = arr.count
    local out = #()
    for i = 1 to n do (
        local w = for k = (i - 2) to (i + 2) collect arr[mcb_clamp k 1 n]
        local sw = sort w
        append out sw[3]
    )
    out
)

-- quaternion pour biped.setTransform (convention vérifiée sur le Biped au démarrage)
fn mcb_toQuat W inv = (
    local q = W as quat
    if inv then inverse q else q
)

-- nom court d'un os Mixamo : « mixamorig:LeftArm », « mixamorig1:LeftArm », « mixamorig_LeftArm » -> « LeftArm »
fn mcb_shortName n = (
    local s = n.name
    local i = s.count
    while i > 0 and s[i] != ":" do i -= 1
    if i > 0 do s = substring s (i + 1) (-1)
    if (findString (toLower s) "mixamorig") == 1 do (
        local k = 10
        while k <= s.count and (findString "0123456789" s[k]) != undefined do k += 1
        if k <= s.count and (s[k] == "_" or s[k] == ":") do k += 1
        s = substring s k (-1)
    )
    s
)

fn mcb_allNodes n arr = (
    append arr n
    for c in n.children do mcb_allNodes c arr
    arr
)

-- chaînes de phalanges (doigts ou orteils) sous la main / le pied : #( #(base, ph2, ph3), ... )
fn mcb_chains ctrl part root = (
    local nodes = #()
    local nl = biped.maxNumLinks ctrl
    for k = 1 to nl do (
        local n = biped.getNode ctrl part link:k
        if n != undefined do append nodes n
    )
    local chains = #()
    for n in nodes do (
        if n.parent == root then append chains #(n)
        else (
            for c in chains do (if c[c.count] == n.parent do append c n)
        )
    )
    chains
)

-- rotation cible d'un nœud du Biped à l'image courante : calculée, sinon rotation locale de repos
fn mcb_tgtOf n tgtN tgtM bipAll bipW0 = (
    local i = findItem tgtN n
    if i > 0 then tgtM[i] else (
        local k = findItem bipAll n
        local pa = n.parent
        if pa == undefined or (findItem bipAll pa) == 0 then bipW0[k] else (
            (bipW0[k] * (mcb_tr bipW0[findItem bipAll pa])) * (mcb_tgtOf pa tgtN tgtM bipAll bipW0)
        )
    )
)

fn mcb_findCom n = (
    local c = n
    while c.parent != undefined do c = c.parent
    if (classOf c.transform.controller) == Vertical_Horizontal_Turn then c else undefined
)

-- ============================================================================ transfert
fn mcb_transfer srcAny com f0 f1 clearAnim doFingers keepPlace = (
    local ctrl = com.transform.controller
    local fps = frameRate as float
    local msgLog = ""

    -- ---------------------------------------------------------------- source Mixamo
    local root = srcAny
    while root.parent != undefined do root = root.parent
    local allSrc = mcb_allNodes root #()
    local shorts = for n in allSrc collect (mcb_shortName n)
    local SIDES = #("Left", "Right")
    local FING = #("Thumb", "Index", "Middle", "Ring", "Pinky")
    local need = #("Hips", "Spine", "Spine1", "Spine2", "Neck", "Head", "HeadTop_End")
    for sd in SIDES do (
        for b in #("Shoulder", "Arm", "ForeArm", "Hand", "UpLeg", "Leg", "Foot", "ToeBase", "Toe_End") do append need (sd + b)
        for f in FING do (for j = 1 to 4 do append need (sd + "Hand" + f + (j as string)))
    )
    local snames = #()
    local snodes = #()
    for nm in need do (
        local i = findItem shorts nm
        if i > 0 do (append snames nm; append snodes allSrc[i])
    )
    local required = #("Hips", "Spine", "Neck", "Head", "LeftArm", "LeftForeArm", "LeftHand", "RightArm",
                       "RightForeArm", "RightHand", "LeftUpLeg", "LeftLeg", "LeftFoot", "RightUpLeg", "RightLeg", "RightFoot")
    local missing = for nm in required where (findItem snames nm) == 0 collect nm
    if missing.count > 0 do (
        local m = ""
        for x in missing do m += x + " "
        return ("Squelette Mixamo incomplet, os introuvables : " + m)
    )
    local iHips = findItem snames "Hips"
    local iNeck = findItem snames "Neck"
    local iHead = findItem snames "Head"
    local iTop = findItem snames "HeadTop_End"
    local spSrc = for nm in #("Spine", "Spine1", "Spine2") where (findItem snames nm) > 0 collect (findItem snames nm)
    local iLU = findItem snames "LeftUpLeg"
    local iRU = findItem snames "RightUpLeg"
    local iLA = findItem snames "LeftArm"
    local iRA = findItem snames "RightArm"

    -- lecture de toute l'animation source
    local nF = f1 - f0 + 1
    local P = #()
    local R = #()
    progressStart "MocapCam : lecture de l'animation Mixamo…"
    for ti = 1 to nF do (
        at time (f0 + ti - 1) (
            append P (for n in snodes collect n.transform.pos)
            append R (for n in snodes collect (mcb_rot n.transform))
        )
        if (mod ti 50) == 0 do progressUpdate (100.0 * ti / nF)
    )
    progressEnd()

    -- ---------------------------------------------------------------- références calibrées de la source
    -- (vecteur moyen exprimé dans le repère local de l'os : il tourne ensuite exactement avec l'os,
    -- sans dépendre d'une T-pose ni de l'orientation des axes de l'os)
    local wSpine = #(0.35, 0.65, 0.9)
    local spRef = #()
    for s = 1 to spSrc.count do (
        local w = wSpine[mcb_clamp (s + 3 - spSrc.count) 1 3]
        local acc = [0, 0, 0]
        for ti = 1 to nF do (
            local lat = mcb_nrm ((mcb_nrm (P[ti][iLU] - P[ti][iRU])) * (1.0 - w) + (mcb_nrm (P[ti][iLA] - P[ti][iRA])) * w)
            acc += lat * (mcb_tr R[ti][spSrc[s]])
        )
        append spRef (mcb_nrm acc)
    )
    local neckRef = [0, 0, 0]
    local headRef = [0, 0, 0]
    for ti = 1 to nF do (
        local lat = mcb_nrm (P[ti][iLA] - P[ti][iRA])
        neckRef += lat * (mcb_tr R[ti][iNeck])
        headRef += lat * (mcb_tr R[ti][iHead])
    )
    neckRef = mcb_nrm neckRef
    headRef = mcb_nrm headRef

    -- par côté : axes du coude, du genou, des doigts ; références du pied et de l'orteil
    local S_sh = #(0, 0); local S_arm = #(0, 0); local S_fa = #(0, 0); local S_hd = #(0, 0)
    local S_ul = #(0, 0); local S_lg = #(0, 0); local S_ft = #(0, 0); local S_tb = #(0, 0); local S_te = #(0, 0)
    local elbowRef = #([0,0,0], [0,0,0]); local kneeRef = #([0,0,0], [0,0,0])
    local footRef = #([0,0,0], [0,0,0]); local toeRef = #([0,0,0], [0,0,0])
    local fingIdx = #(#(), #())     -- [côté][doigt] = #(i1, i2, i3, i4) (i4 peut valoir 0)
    local fingRef = #(#(), #())
    local hasHandFrame = #(false, false)
    for s = 1 to 2 do (
        local sd = SIDES[s]
        S_sh[s] = findItem snames (sd + "Shoulder"); S_arm[s] = findItem snames (sd + "Arm")
        S_fa[s] = findItem snames (sd + "ForeArm"); S_hd[s] = findItem snames (sd + "Hand")
        S_ul[s] = findItem snames (sd + "UpLeg"); S_lg[s] = findItem snames (sd + "Leg")
        S_ft[s] = findItem snames (sd + "Foot"); S_tb[s] = findItem snames (sd + "ToeBase")
        S_te[s] = findItem snames (sd + "Toe_End")
        -- coude et genou : axe moyen des images où l'articulation est nettement pliée
        local chainsJ = #(#(S_arm[s], S_fa[s], S_hd[s], 1.0), #(S_ul[s], S_lg[s], S_ft[s], -1.0))
        for c = 1 to 2 do (
            local a = chainsJ[c][1]; local b = chainsJ[c][2]; local ie = chainsJ[c][3]
            local acc = [0, 0, 0]
            local cnt = 0
            for ti = 1 to nF do (
                local h = cross (mcb_nrm (P[ti][b] - P[ti][a])) (mcb_nrm (P[ti][ie] - P[ti][b]))
                local sl = length h
                if sl > (sin 20.0) do (acc += (h / sl) * (mcb_tr R[ti][a]); cnt += 1)
            )
            if cnt < 10 do (     -- jamais plié : axe anatomique (coude vers l'avant, genou vers l'arrière)
                acc = [0, 0, 0]
                for ti = 1 to nF do (
                    local F = mcb_nrm (cross (P[ti][iLU] - P[ti][iRU]) (P[ti][spSrc[1]] - P[ti][iHips]))
                    acc += (mcb_nrm (cross (P[ti][b] - P[ti][a]) (F * chainsJ[c][4]))) * (mcb_tr R[ti][a])
                )
            )
            if c == 1 then (elbowRef[s] = mcb_nrm acc) else (kneeRef[s] = mcb_nrm acc)
            msgLog += sd + (if c == 1 then " coude" else " genou") + " : axe calé sur " + (cnt as string) + " images pliées\n"
        )
        -- pied et orteil : ligne des hanches dans leur repère
        if S_tb[s] > 0 do (
            local accF = [0, 0, 0]
            local accT = [0, 0, 0]
            for ti = 1 to nF do (
                local lat = mcb_nrm (P[ti][iLU] - P[ti][iRU])
                accF += lat * (mcb_tr R[ti][S_ft[s]])
                accT += lat * (mcb_tr R[ti][S_tb[s]])
            )
            footRef[s] = mcb_nrm accF
            toeRef[s] = mcb_nrm accT
        )
        -- doigts
        for f in FING do (
            local ids = for j = 1 to 4 collect (findItem snames (sd + "Hand" + f + (j as string)))
            append fingIdx[s] ids
        )
        local iM = fingIdx[s][3][1]; local iI = fingIdx[s][2][1]; local iP = fingIdx[s][5][1]
        hasHandFrame[s] = iM > 0 and iI > 0 and iP > 0
        local sgn = if s == 1 then 1.0 else -1.0
        for k = 1 to 5 do (
            local ids = fingIdx[s][k]
            if ids[1] > 0 and ids[2] > 0 and ids[3] > 0 then (
                local acc = [0, 0, 0]
                local cnt = 0
                for ti = 1 to nF do (
                    local h = cross (mcb_nrm (P[ti][ids[2]] - P[ti][ids[1]])) (mcb_nrm (P[ti][ids[3]] - P[ti][ids[2]]))
                    local sl = length h
                    if sl > (sin 20.0) do (acc += (h / sl) * (mcb_tr R[ti][ids[1]]); cnt += 1)
                )
                if cnt < 10 and hasHandFrame[s] do (   -- jamais plié : vers la paume
                    acc = [0, 0, 0]
                    for ti = 1 to nF do (
                        local palm = (cross (mcb_nrm (P[ti][iM] - P[ti][S_hd[s]])) (mcb_nrm (P[ti][iI] - P[ti][iP]))) * sgn
                        acc += (mcb_nrm (cross (P[ti][ids[2]] - P[ti][ids[1]]) palm)) * (mcb_tr R[ti][ids[1]])
                    )
                )
                append fingRef[s] (mcb_nrm acc)
            ) else append fingRef[s] undefined
        )
    )
    -- main sans doigts : référence « avant » solidaire de la main
    local handRef = #([0,0,0], [0,0,0])
    for s = 1 to 2 where (not hasHandFrame[s]) do (
        local acc = [0, 0, 0]
        for ti = 1 to nF do (
            local F = mcb_nrm (cross (P[ti][iLU] - P[ti][iRU]) (P[ti][spSrc[1]] - P[ti][iHips]))
            acc += F * (mcb_tr R[ti][S_hd[s]])
        )
        handRef[s] = mcb_nrm acc
    )

    -- ---------------------------------------------------------------- Biped
    local bipAll = mcb_allNodes com #()
    local pelvis = biped.getNode ctrl #pelvis
    local spB = for k = 1 to ctrl.spineLinks collect (biped.getNode ctrl #spine link:k)
    spB = for n in spB where n != undefined collect n
    local neckB = for k = 1 to ctrl.neckLinks collect (biped.getNode ctrl #neck link:k)
    neckB = for n in neckB where n != undefined collect n
    local headB = biped.getNode ctrl #head
    local armParts = #(#larm, #rarm)
    local legParts = #(#lleg, #rleg)
    local fingParts = #(#lfingers, #rfingers)
    local toeParts = #(#ltoes, #rtoes)
    local B_cl = #(undefined, undefined); local B_up = #(undefined, undefined)
    local B_fa = #(undefined, undefined); local B_hd = #(undefined, undefined)
    local B_th = #(undefined, undefined); local B_ca = #(undefined, undefined); local B_ft = #(undefined, undefined)
    local B_fg = #(#(), #()); local B_toe = #(#(), #())
    for s = 1 to 2 do (
        B_cl[s] = biped.getNode ctrl armParts[s] link:1
        B_up[s] = biped.getNode ctrl armParts[s] link:2
        B_fa[s] = biped.getNode ctrl armParts[s] link:3
        B_hd[s] = biped.getNode ctrl armParts[s] link:4
        local legNodes = for k = 1 to 4 collect (biped.getNode ctrl legParts[s] link:k)
        legNodes = for n in legNodes where n != undefined collect n
        B_th[s] = legNodes[1]; B_ca[s] = legNodes[2]; B_ft[s] = legNodes[legNodes.count]
        if B_hd[s] != undefined do B_fg[s] = mcb_chains ctrl fingParts[s] B_hd[s]
        if B_ft[s] != undefined do B_toe[s] = mcb_chains ctrl toeParts[s] B_ft[s]
    )
    if B_th[1] == undefined or B_th[2] == undefined or spB.count == 0 do return "Biped sans jambes ou sans colonne : non pris en charge."

    -- pose de repos : celle du mode Figure
    local wasFig = ctrl.figureMode
    ctrl.figureMode = true
    local bipW0 = for n in bipAll collect (mcb_rot n.transform)
    local bipP0 = for n in bipAll collect n.transform.pos
    ctrl.figureMode = wasFig
    fn W0of n bipAll bipW0 = bipW0[findItem bipAll n]
    fn P0of n bipAll bipP0 = bipP0[findItem bipAll n]

    local thL = P0of B_th[1] bipAll bipP0
    local thR = P0of B_th[2] bipAll bipP0
    local midB0 = (thL + thR) / 2.0
    local Lb = mcb_nrm (thL - thR)
    local Ub = mcb_nrm ((P0of spB[1] bipAll bipP0) - midB0)
    local Fb = mcb_nrm (cross Lb Ub)
    local Wcom0 = W0of com bipAll bipW0
    local Pcom0 = P0of com bipAll bipP0
    local FrCom = mcb_frame (thL - thR) ((P0of spB[1] bipAll bipP0) - midB0)

    -- correspondance doigts Biped -> doigts Mixamo selon leur nombre
    local fmap = #(#(3), #(1, 3), #(1, 2, 3), #(1, 2, 3, 4), #(1, 2, 3, 4, 5))
    -- axes de charnière du Biped au repos, orientés dans le sens anatomique
    local hbArm = #([0,0,0], [0,0,0]); local hbLeg = #([0,0,0], [0,0,0])
    local hbF = #(#(), #()); local FrHand = #(undefined, undefined)
    for s = 1 to 2 do (
        local sgn = if s == 1 then 1.0 else -1.0
        if B_fa[s] != undefined do (
            local z = (W0of B_fa[s] bipAll bipW0).row3
            local x = (W0of B_up[s] bipAll bipW0).row1
            hbArm[s] = if (dot z (cross x Fb)) >= 0 then z else [0, 0, 0] - z
        )
        local zc = (W0of B_ca[s] bipAll bipW0).row3
        local xt = (W0of B_th[s] bipAll bipW0).row1
        hbLeg[s] = if (dot zc (cross xt ([0, 0, 0] - Fb))) >= 0 then zc else [0, 0, 0] - zc
        if B_hd[s] != undefined do (
            local hp0 = P0of B_hd[s] bipAll bipP0
            local ch = B_fg[s]
            local nfg = ch.count
            local fm = if nfg > 0 then fmap[mcb_clamp nfg 1 5] else #()
            local dh = (W0of B_hd[s] bipAll bipW0).row1
            local kn = Fb
            local iMid = findItem fm 3
            if iMid > 0 do dh = (P0of ch[iMid][1] bipAll bipP0) - hp0
            if nfg >= 4 do kn = (P0of ch[2][1] bipAll bipP0) - (P0of ch[nfg][1] bipAll bipP0)
            FrHand[s] = mcb_frame dh kn
            local palmB = (cross (mcb_nrm dh) (mcb_perp kn (mcb_nrm dh))) * sgn
            for c = 1 to nfg do (
                local x0 = (W0of ch[c][1] bipAll bipW0).row1
                local want = cross x0 palmB
                local hz = if ch[c].count >= 2 then (W0of ch[c][2] bipAll bipW0).row3 else mcb_nrm want
                append hbF[s] (if (dot hz want) >= 0 then hz else [0, 0, 0] - hz)
            )
        )
    )

    -- échelle : longueur des jambes
    local legB = (length ((P0of B_ca[1] bipAll bipP0) - thL)) + (length ((P0of B_ft[1] bipAll bipP0) - (P0of B_ca[1] bipAll bipP0)))
    local legS = (length (P[1][S_lg[1]] - P[1][S_ul[1]])) + (length (P[1][S_ft[1]] - P[1][S_lg[1]]))
    local sc = legB / legS
    local ankZ = for ti = 1 to nF collect (amin #(P[ti][S_ft[1]].z, P[ti][S_ft[2]].z))
    local standZ = (mcb_pct ankZ 5) + legS       -- hauteur des hanches de la source debout
    local mid0 = (P[1][iLU] + P[1][iRU]) / 2.0
    msgLog += "Échelle Biped / source : " + (sc as string) + "\n"

    -- ---------------------------------------------------------------- flexions des doigts
    -- calculées sur toute la prise dans le plan de la charnière, bornées, sans saut, filtrées
    local curl = #(#(), #())       -- [côté][doigt] = #(H (axe par image), K1, K2) ou undefined
    local maxStep = 1200.0 / fps
    if doFingers do (
        for s = 1 to 2 do (
            for k = 1 to 5 do (
                local ids = fingIdx[s][k]
                if fingRef[s][k] == undefined then append curl[s] undefined else (
                    local axes = #(); local C1 = #(); local C2 = #()
                    for ti = 1 to nF do (
                        local d0 = mcb_nrm (P[ti][ids[2]] - P[ti][ids[1]])
                        local d1 = mcb_nrm (P[ti][ids[3]] - P[ti][ids[2]])
                        local d2 = if ids[4] > 0 then (mcb_nrm (P[ti][ids[4]] - P[ti][ids[3]])) else d1
                        local h = mcb_perp (fingRef[s][k] * R[ti][ids[1]]) d0
                        append axes h
                        local a0 = mcb_perp d0 h
                        append C1 (atan2 (dot d1 (cross h a0)) (dot d1 a0))
                        local a1 = mcb_perp d1 h
                        append C2 (atan2 (dot d2 (cross h a1)) (dot d2 a1))
                    )
                    for serie in #(C1, C2) do (
                        local prev = undefined
                        for ti = 1 to nF do (
                            local cv = serie[ti]
                            if cv < -25.0 or cv > 120.0 do (
                                if prev == undefined then (
                                    cv = if (mod (-25.0 - cv + 720.0) 360.0) < (mod (cv - 120.0 + 720.0) 360.0) then -25.0 else 120.0
                                ) else (
                                    cv = if (abs (prev + 25.0)) <= (abs (prev - 120.0)) then -25.0 else 120.0
                                )
                            )
                            if prev != undefined do cv = prev + (mcb_clamp (cv - prev) (-maxStep) maxStep)
                            serie[ti] = cv
                            prev = cv
                        )
                    )
                    C1 = mcb_median5 C1
                    C2 = mcb_median5 C2
                    local K2 = for ti = 1 to nF collect (C1[ti] + C2[ti])
                    append curl[s] #(axes, C1, K2)
                )
            )
        )
    )

    -- ---------------------------------------------------------------- convention des quaternions du Biped
    local qInv = false
    (
        local probe = B_up[1]
        if probe == undefined do probe = B_th[1]
        local qb = biped.getTransform probe #rotation
        local qm = (mcb_rot probe.transform) as quat
        local qi = inverse qm
        local d1 = abs (qb.x * qm.x + qb.y * qm.y + qb.z * qm.z + qb.w * qm.w)
        local d2 = abs (qb.x * qi.x + qb.y * qi.y + qb.z * qi.z + qb.w * qi.w)
        qInv = d2 > d1 + 1e-4
    )

    -- ---------------------------------------------------------------- application
    if clearAnim do biped.clearAllAnimation ctrl
    ctrl.figureMode = false
    local keyed = 0
    local midTarget = #()
    local sampleEvery = amax #(1, nF / 12)
    local sampT = #(); local sampN = #(); local sampM = #()
    local backTime = sliderTime
    local cancelled = false
    disableSceneRedraw()
    try (
    undo off (
    progressStart "MocapCam : transfert vers le Biped…"
    for ti = 1 to nF while (not cancelled) do (
        local t = f0 + ti - 1
        sliderTime = t
        local tgtN = #()
        local tgtM = #()
        local isSample = (mod (ti - 1) sampleEvery) == 0

        -- COM : repère du bassin (ligne des hanches + direction de la colonne)
        local mid = (P[ti][iLU] + P[ti][iRU]) / 2.0
        local Ft = mcb_frame (P[ti][iLU] - P[ti][iRU]) (P[ti][spSrc[1]] - mid)
        local Wc = Wcom0 * (mcb_tr FrCom) * Ft
        local mt = [0, 0, 0]
        if keepPlace then (
            mt = midB0 + (mid - mid0) * sc
            mt.z = midB0.z + (mid.z - standZ) * sc
        ) else (
            mt = mid * sc
            mt.z = midB0.z + (mid.z - standZ) * sc
        )
        append midTarget mt
        local pc = mt + (Pcom0 - midB0) * (mcb_tr Wcom0) * Wc
        biped.setTransform com #pos pc true
        biped.setTransform com #rotation (mcb_toQuat Wc qInv) true
        append tgtN com; append tgtM Wc

        -- colonne, cou, tête
        for i = 1 to spB.count do (
            local s = ((floor ((i - 0.5) * spSrc.count / spB.count)) as integer) + 1
            s = mcb_clamp s 1 spSrc.count
            local a = spSrc[s]
            local b = if s < spSrc.count then spSrc[s + 1] else iNeck
            local W0 = W0of spB[i] bipAll bipW0
            local W = W0 * (mcb_tr (mcb_frame W0.row1 Lb)) * (mcb_frame (P[ti][b] - P[ti][a]) (spRef[s] * R[ti][a]))
            biped.setTransform spB[i] #rotation (mcb_toQuat W qInv) true
            append tgtN spB[i]; append tgtM W
        )
        for n in neckB do (
            local W0 = W0of n bipAll bipW0
            local W = W0 * (mcb_tr (mcb_frame W0.row1 Lb)) * (mcb_frame (P[ti][iHead] - P[ti][iNeck]) (neckRef * R[ti][iNeck]))
            biped.setTransform n #rotation (mcb_toQuat W qInv) true
            append tgtN n; append tgtM W
        )
        if headB != undefined do (
            local dHead = if iTop > 0 then (P[ti][iTop] - P[ti][iHead]) else (P[ti][iHead] - P[ti][iNeck])
            local W0 = W0of headB bipAll bipW0
            local W = W0 * (mcb_tr (mcb_frame W0.row1 Lb)) * (mcb_frame dHead (headRef * R[ti][iHead]))
            biped.setTransform headB #rotation (mcb_toQuat W qInv) true
            append tgtN headB; append tgtM W
        )

        for s = 1 to 2 do (
            -- clavicule : direction seule
            if B_cl[s] != undefined and S_sh[s] > 0 do (
                local pa = B_cl[s].parent
                local M0 = ((W0of B_cl[s] bipAll bipW0) * (mcb_tr (W0of pa bipAll bipW0))) * (mcb_tgtOf pa tgtN tgtM bipAll bipW0)
                local W = mcb_swing M0 (P[ti][S_arm[s]] - P[ti][S_sh[s]])
                biped.setTransform B_cl[s] #rotation (mcb_toQuat W qInv) true
                append tgtN B_cl[s]; append tgtM W
            )
            -- bras et avant-bras : le plan du coude du Biped est placé dans celui de l'animation
            if B_up[s] != undefined do (
                local d1 = P[ti][S_fa[s]] - P[ti][S_arm[s]]
                local d2 = P[ti][S_hd[s]] - P[ti][S_fa[s]]
                local h = mcb_hingeDir d1 d2 (elbowRef[s] * R[ti][S_arm[s]])
                local W0 = W0of B_up[s] bipAll bipW0
                local Wu = W0 * (mcb_tr (mcb_frame W0.row1 hbArm[s])) * (mcb_frame d1 h)
                biped.setTransform B_up[s] #rotation (mcb_toQuat Wu qInv) true
                append tgtN B_up[s]; append tgtM Wu
                local M0 = ((W0of B_fa[s] bipAll bipW0) * (mcb_tr W0)) * Wu
                local Wf = mcb_hinge M0 d2 h (-30.0) 170.0
                biped.setTransform B_fa[s] #rotation (mcb_toQuat Wf qInv) true
                append tgtN B_fa[s]; append tgtM Wf
                -- main
                local Ftm = undefined
                if hasHandFrame[s] then (
                    local ids = fingIdx[s]
                    Ftm = mcb_frame (P[ti][ids[3][1]] - P[ti][S_hd[s]]) (P[ti][ids[2][1]] - P[ti][ids[5][1]])
                ) else (
                    Ftm = mcb_frame d2 (handRef[s] * R[ti][S_hd[s]])
                )
                local Wh = (W0of B_hd[s] bipAll bipW0) * (mcb_tr FrHand[s]) * Ftm
                biped.setTransform B_hd[s] #rotation (mcb_toQuat Wh qInv) true
                append tgtN B_hd[s]; append tgtM Wh
                -- doigts
                if doFingers do (
                    local ch = B_fg[s]
                    local nfg = ch.count
                    local fm = if nfg > 0 then fmap[mcb_clamp nfg 1 5] else #()
                    for c = 1 to nfg do (
                        local k = fm[c]
                        local cu = curl[s][k]
                        if cu != undefined do (
                            local ids = fingIdx[s][k]
                            local d0 = P[ti][ids[2]] - P[ti][ids[1]]
                            local hF = cu[1][ti]
                            local Wb0 = W0of ch[c][1] bipAll bipW0
                            local W = Wb0 * (mcb_tr (mcb_frame Wb0.row1 hbF[s][c])) * (mcb_frame d0 hF)
                            biped.setTransform ch[c][1] #rotation (mcb_toQuat W qInv) true
                            append tgtN ch[c][1]; append tgtM W
                            local nl = ch[c].count
                            for j = 2 to nl do (
                                local sj = if nl == 2 then 2 else ((floor ((j - 1) * 2.0 / (nl - 1) + 0.5)) as integer)
                                local ang = if sj <= 0 then 0.0 else (if sj == 1 then cu[2][ti] else cu[3][ti])
                                local dj = mcb_rotAbout (mcb_nrm d0) hF ang
                                local M0 = ((W0of ch[c][j] bipAll bipW0) * (mcb_tr (W0of ch[c][j - 1] bipAll bipW0))) * W
                                W = mcb_hinge M0 dj hF (-180.0) 180.0
                                biped.setTransform ch[c][j] #rotation (mcb_toQuat W qInv) true
                                append tgtN ch[c][j]; append tgtM W
                            )
                        )
                    )
                )
            )
            -- jambe : le plan du genou du Biped est placé dans celui de l'animation
            local d1 = P[ti][S_lg[s]] - P[ti][S_ul[s]]
            local d2 = P[ti][S_ft[s]] - P[ti][S_lg[s]]
            local h = mcb_hingeDir d1 d2 (kneeRef[s] * R[ti][S_ul[s]])
            local W0 = W0of B_th[s] bipAll bipW0
            local Wt = W0 * (mcb_tr (mcb_frame W0.row1 hbLeg[s])) * (mcb_frame d1 h)
            biped.setTransform B_th[s] #rotation (mcb_toQuat Wt qInv) true
            append tgtN B_th[s]; append tgtM Wt
            local M0 = ((W0of B_ca[s] bipAll bipW0) * (mcb_tr W0)) * Wt
            local Wk = mcb_hinge M0 d2 h (-30.0) 170.0
            biped.setTransform B_ca[s] #rotation (mcb_toQuat Wk qInv) true
            append tgtN B_ca[s]; append tgtM Wk
            -- pied et orteils
            if S_tb[s] > 0 do (
                local toe0 = if B_toe[s].count > 0 then B_toe[s][1][1] else undefined
                local Wf0 = W0of B_ft[s] bipAll bipW0
                local dFb = if toe0 != undefined then ((P0of toe0 bipAll bipP0) - (P0of B_ft[s] bipAll bipP0)) else Wf0.row1
                local Wfo = Wf0 * (mcb_tr (mcb_frame dFb Lb)) * (mcb_frame (P[ti][S_tb[s]] - P[ti][S_ft[s]]) (footRef[s] * R[ti][S_ft[s]]))
                biped.setTransform B_ft[s] #rotation (mcb_toQuat Wfo qInv) true
                append tgtN B_ft[s]; append tgtM Wfo
                local dT = if S_te[s] > 0 then (P[ti][S_te[s]] - P[ti][S_tb[s]]) else (P[ti][S_tb[s]] - P[ti][S_ft[s]])
                for c in B_toe[s] do (
                    local Wt0 = W0of c[1] bipAll bipW0
                    local W = Wt0 * (mcb_tr (mcb_frame Wt0.row1 Lb)) * (mcb_frame dT (toeRef[s] * R[ti][S_tb[s]]))
                    biped.setTransform c[1] #rotation (mcb_toQuat W qInv) true
                    append tgtN c[1]; append tgtM W
                )
            )
        )
        keyed = tgtN.count
        if isSample do (
            for i = 1 to tgtN.count do (append sampT t; append sampN tgtN[i]; append sampM tgtM[i])
        )
        if (mod ti 10) == 0 do (
            if not (progressUpdate (100.0 * ti / nF)) do cancelled = true
            if getProgressCancel() do cancelled = true
        )
    )
    progressEnd()

    -- ---------------------------------------------------------------- recalage du bassin
    -- (le Biped peut décaler son bassin par rapport au centre de masse : on corrige la position
    -- du COM pour que les hanches soient exactement là où elles doivent être)
    if not cancelled do (
        progressStart "MocapCam : recalage des hanches…"
        for pass = 1 to 2 do (
            for ti = 1 to nF do (
                local t = f0 + ti - 1
                sliderTime = t
                local actual = (B_th[1].transform.pos + B_th[2].transform.pos) / 2.0
                local dlt = midTarget[ti] - actual
                if (length dlt) > (legB * 0.001) do biped.setTransform com #pos (com.transform.pos + dlt) true
                if (mod ti 20) == 0 do progressUpdate (50.0 * (pass - 1) + 50.0 * ti / nF)
            )
        )
        progressEnd()
    )
    )   -- undo off
    ) catch (
        enableSceneRedraw()
        progressEnd()
        sliderTime = backTime
        throw()
    )
    enableSceneRedraw()
    sliderTime = backTime

    -- ---------------------------------------------------------------- vérification
    local worst = #()
    local names = #()
    for i = 1 to sampT.count do (
        local W = at time sampT[i] (mcb_rot sampN[i].transform)
        local er = mcb_rotAngle W sampM[i]
        local k = findItem names sampN[i].name
        if k == 0 then (append names sampN[i].name; append worst er) else (if er > worst[k] do worst[k] = er)
    )
    local order = for i = 1 to worst.count collect i
    fn cmpW a b worst: = (if worst[a] > worst[b] then -1 else (if worst[a] < worst[b] then 1 else 0))
    qsort order cmpW worst:worst
    local rep = (if cancelled then "Transfert interrompu.\n" else "") + \
        (nF as string) + " images, " + (keyed as string) + " os du Biped animés, échelle " + \
        ((formattedPrint sc format:".2f") as string) + "\n"
    if worst.count > 0 do (
        rep += "Écart avec la cible (images témoins) : "
        for i = 1 to (amin #(3, order.count)) do rep += names[order[i]] + " " + ((formattedPrint worst[order[i]] format:".1f") as string) + "°  "
        rep += "\n"
    )
    format "%\n%\n" msgLog rep
    rep
)

-- ============================================================================ interface
rollout MocapCamBipedRollout "MocapCam -> Biped" width:320
(
    local srcNode = undefined
    local comNode = undefined

    group "1. Animation Mixamo (FBX importé)"
    (
        pickbutton pbSrc "Choisir un os du squelette Mixamo" width:290 autoDisplay:false
        label lbSrc "" align:#left width:290
    )
    group "2. Biped"
    (
        pickbutton pbBip "Choisir le Biped" width:290 autoDisplay:false
        label lbBip "" align:#left width:290
    )
    group "3. Options"
    (
        spinner spStart "Début " type:#integer range:[-100000, 100000, 0] fieldwidth:55 across:2 align:#left
        spinner spEnd "Fin " type:#integer range:[-100000, 100000, 100] fieldwidth:55 align:#right
        checkbox cbClear "Effacer l'animation existante du Biped" checked:true
        checkbox cbFingers "Doigts" checked:true
        checkbox cbKeep "Partir de la position actuelle du Biped" checked:true \
            tooltip:"Décoché : le Biped se place là où est le squelette Mixamo (à son échelle)."
    )
    button btGo "Transférer l'animation" width:290 height:34
    label lbInfo "" align:#left width:290 height:70

    on pbSrc picked obj do (
        if obj != undefined do (
            local root = obj
            while root.parent != undefined do root = root.parent
            local allN = mcb_allNodes root #()
            local shorts = for n in allN collect (mcb_shortName n)
            if (findItem shorts "Hips") == 0 or (findItem shorts "LeftArm") == 0 then (
                messageBox "Ce n'est pas un squelette Mixamo (os « Hips », « LeftArm »… introuvables)." title:"MocapCam"
            ) else (
                srcNode = obj
                pbSrc.text = root.name
                local nf = 0
                for f in #("Thumb", "Index", "Middle", "Ring", "Pinky") do (if (findItem shorts ("LeftHand" + f + "1")) > 0 do nf += 1)
                lbSrc.text = (allN.count as string) + " os, " + (nf as string) + " doigts par main"
                spStart.value = animationRange.start.frame as integer
                spEnd.value = animationRange.end.frame as integer
            )
        )
    )
    on pbBip picked obj do (
        if obj != undefined do (
            local c = mcb_findCom obj
            if c == undefined then messageBox "Ce n'est pas un Biped." title:"MocapCam" else (
                comNode = c
                pbBip.text = c.name
                local ctrl = c.transform.controller
                lbBip.text = "colonne " + (ctrl.spineLinks as string) + ", cou " + (ctrl.neckLinks as string) + \
                    ", doigts " + (ctrl.fingers as string) + " x " + (ctrl.fingerLinks as string) + \
                    ", orteils " + (ctrl.toes as string)
            )
        )
    )
    on btGo pressed do (
        if srcNode == undefined or (isDeleted srcNode) then messageBox "Choisissez d'abord le squelette Mixamo." title:"MocapCam"
        else if comNode == undefined or (isDeleted comNode) then messageBox "Choisissez d'abord le Biped." title:"MocapCam"
        else if spEnd.value <= spStart.value then messageBox "Plage d'images vide." title:"MocapCam"
        else (
            lbInfo.text = "Transfert en cours…"
            local t0 = timeStamp()
            local msg = mcb_transfer srcNode comNode spStart.value spEnd.value cbClear.checked cbFingers.checked cbKeep.checked
            local dt = (timeStamp() - t0) / 1000.0
            lbInfo.text = msg + "Durée : " + ((formattedPrint dt format:".0f") as string) + " s"
        )
    )
)

-- (mcb_noUI = true avant fileIn : fonctions seules, sans fenêtre, pour les tests en lot)
if mcb_noUI != true do (
    try (destroyDialog MocapCamBipedRollout) catch ()
    createDialog MocapCamBipedRollout
)
)
