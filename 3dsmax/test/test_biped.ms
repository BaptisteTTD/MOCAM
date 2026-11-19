-- Test en lot (3dsmaxbatch) : import du FBX MOCAM, Biped neuf, transfert, mesures.
(
local dir = getFilenamePath (getSourceFileName())
local logf = createFile (dir + "resultat.txt")
fn say f msg = (format "%\n" msg to:f; flush f; format "%\n" msg)
try (
    say logf ("3ds Max " + ((maxVersion())[1] as string))
    resetMaxFile #noPrompt
    global mcb_noUI = true
    fileIn (dir + "..\\MocapCam_Biped.ms")
    say logf "script chargé"
    local fbx = dir + "..\\..\\captures\\prise_006\\prise_006.fbx"
    FBXImporterSetParam "Animation" true
    importFile fbx #noPrompt using:FBXIMP
    local hips = undefined
    for o in objects where (mcb_shortName o) == "Hips" do hips = o
    say logf ("import : " + (objects.count as string) + " objets, Hips = " + (hips as string) + ", plage " + (animationRange as string))
    local cases = #(#(3, 1, 5, 3, "complet"), #(4, 1, 1, 1, "defaut"))
    for cs in cases do (
        local bip = biped.createNew 180 (-90) [300, 0, 100] arms:true neckLinks:cs[2] spineLinks:cs[1] legLinks:3 fingers:cs[3] fingerLinks:cs[4] toes:1 toeLinks:1
        local ctrl = bip.transform.controller
        say logf ("Biped " + cs[5] + " : " + bip.name)
        local t0 = timeStamp()
        local f0 = animationRange.start.frame as integer
        local f1 = animationRange.end.frame as integer
        local msg = mcb_transfer hips bip f0 f1 true true true
        say logf msg
        say logf ("durée " + (((timeStamp() - t0) / 1000.0) as string) + " s")
        -- mesures : direction des os du Biped vs os Mixamo
        local pairs = #(#(#larm, 2, "LeftArm", "LeftForeArm"), #(#larm, 3, "LeftForeArm", "LeftHand"),
                        #(#rarm, 2, "RightArm", "RightForeArm"), #(#rarm, 3, "RightForeArm", "RightHand"),
                        #(#lleg, 1, "LeftUpLeg", "LeftLeg"), #(#lleg, 2, "LeftLeg", "LeftFoot"),
                        #(#rleg, 1, "RightUpLeg", "RightLeg"), #(#spine, 1, "Spine", "Spine1"), #(#head, 1, "Head", "HeadTop_End"))
        local srcByName = #(); local srcN = #()
        for o in objects do (append srcByName (mcb_shortName o); append srcN o)
        for pr in pairs do (
            local bn = biped.getNode ctrl pr[1] link:pr[2]
            local a = srcN[findItem srcByName pr[3]]
            local b = srcN[findItem srcByName pr[4]]
            local errs = #()
            for f = f0 to f1 by 10 do at time f (
                append errs (acos (mcb_clamp (dot (mcb_nrm bn.transform.row1) (mcb_nrm (b.transform.pos - a.transform.pos))) (-1.0) 1.0))
            )
            say logf ("  " + bn.name + " direction : médiane " + ((mcb_pct errs 50) as string) + " max " + ((mcb_pct errs 100) as string))
        )
        -- sauts de rotation d'une image à l'autre
        local worstJ = 0.0; local worstN = ""
        local nodes = for n in (mcb_allNodes bip #()) where (findString n.name "Nub") == undefined and (findString n.name "Footsteps") == undefined collect n
        for n in nodes do (
            local prev = undefined
            for f = f0 to f1 do (
                local W = at time f (mcb_rot n.transform)
                if prev != undefined do (
                    local ej = mcb_rotAngle W prev
                    if ej > worstJ do (worstJ = ej; worstN = n.name + " @" + (f as string))
                )
                prev = W
            )
        )
        say logf ("  plus gros saut d'une image à l'autre : " + (worstJ as string) + "° (" + worstN + ")")
        -- hanches : chemin du Biped vs source (mis à l'échelle)
        local lu = srcN[findItem srcByName "LeftUpLeg"]; local ru = srcN[findItem srcByName "RightUpLeg"]
        local th1 = biped.getNode ctrl #lleg link:1; local th2 = biped.getNode ctrl #rleg link:1
        local ds = #(); local db = #()
        local s0 = at time f0 ((lu.transform.pos + ru.transform.pos) / 2.0)
        local b0 = at time f0 ((th1.transform.pos + th2.transform.pos) / 2.0)
        for f = f0 to f1 by 25 do at time f (
            append ds (length (((lu.transform.pos + ru.transform.pos) / 2.0) - s0))
            append db (length (((th1.transform.pos + th2.transform.pos) / 2.0) - b0))
        )
        local ratio = #()
        for i = 1 to ds.count where ds[i] > 1.0 do append ratio (db[i] / ds[i])
        say logf ("  déplacement des hanches Biped / source : médiane " + ((mcb_pct ratio 50) as string) + " (min " + ((mcb_pct ratio 0) as string) + ", max " + ((mcb_pct ratio 100) as string) + ")")
    )
    saveMaxFile (dir + "test_biped.max") quiet:true
    say logf "OK"
) catch (
    say logf ("ERREUR : " + (getCurrentException() as string))
)
close logf
quitMax #noPrompt
)
